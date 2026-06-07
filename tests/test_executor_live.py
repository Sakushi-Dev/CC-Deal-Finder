"""LiveExecutor tests — the only component that spends real funds.

This is the highest-value module in the suite. It pins, exhaustively, that the
live executor:

* only ever proceeds when the wallet can sign;
* enforces every preflight guard (budget, duplicate, market reference, discount);
* drives the exact state machine SUBMITTED -> SIGNED -> PENDING -> CONFIRMED/OPEN;
* NEVER auto-retries a broadcast (no double-spend) and fails safely on any error;
* spawns a relist follow-up only for a confirmed, resaleable buy;
* keeps the batch going when a single order blows up.
"""
from __future__ import annotations

import pytest

from collectorcrypt.trader import executor as ex
from collectorcrypt.trader.ccapi import (CCNetworkError, CCRateLimitError,
                                         CCServerError)
from collectorcrypt.trader.executor import (DryRunExecutor, LiveExecutor,
                                            _extract_external_id, _extract_signature,
                                            _extract_tx, _first, _is_cancelled,
                                            _is_confirmed, _is_filled)
from collectorcrypt.trader.executor import record_sold_holding
from collectorcrypt.trader.orders import Order, OrderKind, OrderStatus
from collectorcrypt.trader.store import (HOLDING_HELD, HOLDING_LISTED,
                                         HOLDING_SOLD, Holding)

from .conftest import (FakeClient, FakeWallet, make_buy, make_config, make_list,
                       make_offer)


def make_live(client=None, wallet=None, *, store=None, volume=1000.0, cfg=None):
    return LiveExecutor(
        wallet or FakeWallet(can_sign=True),
        "https://rpc.test.invalid",
        client=client or FakeClient(),
        store=store,
        available_volume=volume,
        cfg=cfg or make_config(live=True),
    )


# --------------------------------------------------------------------------- #
# Global preflight: signing wallet required
# --------------------------------------------------------------------------- #
def test_no_signing_wallet_fails_all_orders():
    client = FakeClient()
    executor = make_live(client=client, wallet=FakeWallet(can_sign=False))
    orders = [make_buy(nft="A"), make_offer(nft="B")]
    result = executor.execute(orders)
    assert all(o.status is OrderStatus.FAILED for o in result)


def test_no_signing_wallet_sends_nothing():
    client = FakeClient()
    executor = make_live(client=client, wallet=FakeWallet(can_sign=False))
    executor.execute([make_buy(nft="A")])
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Buy happy path
# --------------------------------------------------------------------------- #
def test_buy_confirmed():
    client = FakeClient()
    executor = make_live(client=client)
    [buy] = [o for o in executor.execute([make_buy(price_usd=10, market_usd=20,
                                                   resell_usd=0)])
             if o.kind is OrderKind.BUY]
    assert buy.status is OrderStatus.CONFIRMED


def test_buy_walks_full_state_machine():
    client = FakeClient()
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    steps = [h["to"] for h in buy.history]
    assert steps == ["submitted", "signed", "pending", "confirmed"]


def test_buy_calls_initiate_then_broadcast():
    client = FakeClient()
    executor = make_live(client=client)
    executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert client.call_names() == ["initiate_buy", "broadcast"]


def test_confirmed_buy_with_resell_spawns_relist():
    client = FakeClient()
    executor = make_live(client=client)
    result = executor.execute([make_buy(price_usd=10, market_usd=20,
                                        resell_usd=18)])
    lists = [o for o in result if o.kind is OrderKind.LIST]
    assert len(lists) == 1
    assert lists[0].status is OrderStatus.PLANNED  # deferred, not sent
    assert lists[0].price_usd == 18


def test_confirmed_buy_without_resell_no_relist():
    client = FakeClient()
    executor = make_live(client=client)
    result = executor.execute([make_buy(price_usd=10, market_usd=20,
                                        resell_usd=0)])
    assert all(o.kind is not OrderKind.LIST for o in result)


def test_buy_pending_when_not_confirmed():
    client = FakeClient()
    client.broadcast_response = {"signature": "S"}  # no confirmation evidence
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.status is OrderStatus.PENDING


def test_buy_records_signature():
    client = FakeClient()
    client.broadcast_response = {"status": "confirmed", "signature": "SIGXYZ"}
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.signature == "SIGXYZ"


def test_buy_records_external_id():
    client = FakeClient()
    client.responses["initiate_buy"] = {"transaction": "tx", "receiptId": "RC9"}
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.external_id == "RC9"


# --------------------------------------------------------------------------- #
# Buy failure paths
# --------------------------------------------------------------------------- #
def test_buy_no_tx_fails():
    client = FakeClient()
    client.responses["initiate_buy"] = {"receiptId": "x"}  # no transaction
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.status is OrderStatus.FAILED
    assert client.count("broadcast") == 0


def test_buy_sign_failure_fails_order():
    client = FakeClient()
    executor = make_live(client=client, wallet=FakeWallet(can_sign=True,
                                                         sign_error=True))
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.status is OrderStatus.FAILED
    assert client.count("broadcast") == 0


@pytest.mark.parametrize("err", [CCServerError("5xx"), CCNetworkError("net"),
                                 CCRateLimitError("429")])
def test_buy_broadcast_error_fails_order_no_retry(err):
    client = FakeClient()
    client.errors["broadcast"] = err
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.status is OrderStatus.FAILED
    assert client.count("broadcast") == 1  # never retried


def test_buy_initiate_error_fails_order():
    client = FakeClient()
    client.errors["initiate_buy"] = CCServerError("down")
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.status is OrderStatus.FAILED


# --------------------------------------------------------------------------- #
# Preflight guards
# --------------------------------------------------------------------------- #
def test_preflight_insufficient_budget_fails():
    client = FakeClient()
    executor = make_live(client=client, volume=5.0)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=20)])
    assert buy.status is OrderStatus.FAILED
    assert client.calls == []  # never sent


def test_preflight_no_market_reference_fails():
    client = FakeClient()
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=10, market_usd=0)])
    assert buy.status is OrderStatus.FAILED
    assert client.calls == []


def test_preflight_price_at_or_above_market_fails():
    client = FakeClient()
    executor = make_live(client=client)
    [buy] = executor.execute([make_buy(price_usd=20, market_usd=20)])
    assert buy.status is OrderStatus.FAILED
    assert client.calls == []


def test_preflight_duplicate_intent_fails(store):
    # Pre-seed a non-PLANNED order with the same client_order_id.
    existing = make_buy(nft="DUP", price_usd=10, market_usd=20, cycle_id="c")
    existing.transition(OrderStatus.CONFIRMED)
    store.upsert_order(existing)

    client = FakeClient()
    executor = make_live(client=client, store=store)
    fresh = make_buy(nft="DUP", price_usd=10, market_usd=20, cycle_id="c")
    [buy] = executor.execute([fresh])
    assert buy.status is OrderStatus.FAILED
    assert client.calls == []


def test_budget_decrements_across_orders():
    client = FakeClient()
    executor = make_live(client=client, volume=15.0)
    result = executor.execute([
        make_buy(nft="A", price_usd=10, market_usd=20),
        make_buy(nft="B", price_usd=10, market_usd=20),
    ])
    buys = [o for o in result if o.kind is OrderKind.BUY]
    statuses = {o.nft: o.status for o in buys}
    assert statuses["A"] is OrderStatus.CONFIRMED
    assert statuses["B"] is OrderStatus.FAILED  # only 5 left after first buy


# --------------------------------------------------------------------------- #
# Offer
# --------------------------------------------------------------------------- #
def test_offer_opens():
    client = FakeClient()
    client.broadcast_response = {"status": "ok"}  # accepted, not filled
    executor = make_live(client=client)
    [offer] = executor.execute([make_offer(price_usd=8, market_usd=20)])
    assert offer.status is OrderStatus.OPEN


def test_offer_filled_confirms():
    client = FakeClient()
    client.broadcast_response = {"status": "filled"}
    executor = make_live(client=client)
    [offer] = executor.execute([make_offer(price_usd=8, market_usd=20)])
    assert offer.status is OrderStatus.CONFIRMED


def test_offer_calls_make_offer():
    client = FakeClient()
    executor = make_live(client=client)
    executor.execute([make_offer(nft="N", price_usd=8, market_usd=20)])
    assert "make_offer" in client.call_names()


def test_offer_make_offer_body_has_card_id_and_wallet():
    client = FakeClient()
    wallet = FakeWallet(can_sign=True)
    executor = make_live(client=client, wallet=wallet)
    executor.execute([make_offer(nft="NFT9", card_id="CARDXYZ",
                                 price_usd=8, market_usd=20, currency="USDC")])
    call = next(kw for name, kw in client.calls if name == "make_offer")
    assert call["card_id"] == "CARDXYZ"
    assert call["nft"] == "NFT9"
    assert call["wallet"] == wallet.address
    assert call["currency"] == "USDC"


def test_offer_without_card_id_fails_safely():
    client = FakeClient()
    executor = make_live(client=client)
    [offer] = executor.execute([make_offer(nft="N", card_id="",
                                           price_usd=8, market_usd=20)])
    assert offer.status is OrderStatus.FAILED
    assert client.calls == []                 # nothing sent, no money moved
    assert "card_id" in offer.detail


def test_offer_no_tx_fails():
    client = FakeClient()
    client.responses["make_offer"] = {"offerId": "x"}
    executor = make_live(client=client)
    [offer] = executor.execute([make_offer(price_usd=8, market_usd=20)])
    assert offer.status is OrderStatus.FAILED


def test_offer_budget_guard():
    client = FakeClient()
    executor = make_live(client=client, volume=5.0)
    [offer] = executor.execute([make_offer(price_usd=8, market_usd=20)])
    assert offer.status is OrderStatus.FAILED


# --------------------------------------------------------------------------- #
# Relist (exit/sell side)
# --------------------------------------------------------------------------- #
def test_relist_confirms():
    client = FakeClient()
    executor = make_live(client=client)
    listing = make_list(nft="N", price_usd=25, cycle_id="c")
    out = executor.relist(listing)
    assert out.status is OrderStatus.CONFIRMED
    assert "create_listing" in client.call_names()


def test_relist_rejects_non_list_order():
    client = FakeClient()
    executor = make_live(client=client)
    out = executor.relist(make_buy(price_usd=10, market_usd=20))
    assert out.status is OrderStatus.FAILED
    assert client.calls == []


def test_relist_requires_signing_wallet():
    client = FakeClient()
    executor = make_live(client=client, wallet=FakeWallet(can_sign=False))
    out = executor.relist(make_list(nft="N", price_usd=25, cycle_id="c"))
    assert out.status is OrderStatus.FAILED


def test_relist_zero_price_fails():
    client = FakeClient()
    executor = make_live(client=client)
    out = executor.relist(make_list(nft="N", price_usd=0, cycle_id="c"))
    assert out.status is OrderStatus.FAILED
    assert client.calls == []


def test_relist_no_tx_fails():
    client = FakeClient()
    client.responses["create_listing"] = {"listingId": "x"}
    executor = make_live(client=client)
    out = executor.relist(make_list(nft="N", price_usd=25, cycle_id="c"))
    assert out.status is OrderStatus.FAILED


def test_relist_never_raises_on_client_error():
    client = FakeClient()
    client.errors["create_listing"] = CCServerError("boom")
    executor = make_live(client=client)
    out = executor.relist(make_list(nft="N", price_usd=25, cycle_id="c"))
    assert out.status is OrderStatus.FAILED


def test_relist_no_budget_check():
    # A relist must succeed even with zero remaining budget (selling, not buying).
    client = FakeClient()
    executor = make_live(client=client, volume=0.0)
    out = executor.relist(make_list(nft="N", price_usd=25, cycle_id="c"))
    assert out.status is OrderStatus.CONFIRMED


# --------------------------------------------------------------------------- #
# Batch resilience + persistence
# --------------------------------------------------------------------------- #
def test_batch_continues_after_one_failure():
    client = FakeClient()
    executor = make_live(client=client)
    result = executor.execute([
        make_buy(nft="BAD", price_usd=20, market_usd=20),   # fails preflight
        make_buy(nft="GOOD", price_usd=10, market_usd=20),  # succeeds
    ])
    statuses = {o.nft: o.status for o in result if o.kind is OrderKind.BUY}
    assert statuses["BAD"] is OrderStatus.FAILED
    assert statuses["GOOD"] is OrderStatus.CONFIRMED


def test_execute_persists_every_transition(store):
    client = FakeClient()
    executor = make_live(client=client, store=store)
    [buy] = [o for o in executor.execute([make_buy(nft="P", price_usd=10,
                                                   market_usd=20, cycle_id="c")])
             if o.kind is OrderKind.BUY]
    saved = store.get_by_client_order_id(buy.client_order_id)
    assert saved is not None
    assert saved.status is OrderStatus.CONFIRMED


def test_returns_same_order_objects():
    client = FakeClient()
    executor = make_live(client=client)
    order = make_buy(price_usd=10, market_usd=20)
    result = executor.execute([order])
    assert order in result


# --------------------------------------------------------------------------- #
# Holdings lifecycle (ETAPPE 5) — populate the ledger on settle
# --------------------------------------------------------------------------- #
def test_confirmed_buy_records_held_holding(store):
    client = FakeClient()
    executor = make_live(client=client, store=store)
    executor.execute([make_buy(nft="HOLD1", name="Card", category="Pokemon",
                               price_usd=10, market_usd=20, cycle_id="c")])
    holding = store.get_holding("HOLD1")
    assert holding is not None
    assert holding.status == HOLDING_HELD
    assert holding.cost_usd == 10
    assert holding.market_usd_at_buy == 20
    assert holding.acquired_at > 0


def test_filled_offer_records_held_holding(store):
    client = FakeClient()
    client.broadcast_response = {"status": "filled"}
    executor = make_live(client=client, store=store)
    [offer] = executor.execute([make_offer(nft="OFF1", price_usd=8,
                                           market_usd=20, cycle_id="c")])
    assert offer.status is OrderStatus.CONFIRMED
    holding = store.get_holding("OFF1")
    assert holding is not None
    assert holding.status == HOLDING_HELD
    assert holding.cost_usd == 8


def test_open_offer_records_no_holding(store):
    client = FakeClient()
    client.broadcast_response = {"status": "ok"}  # accepted onto the book, not filled
    executor = make_live(client=client, store=store)
    [offer] = executor.execute([make_offer(nft="OFF2", price_usd=8,
                                           market_usd=20, cycle_id="c")])
    assert offer.status is OrderStatus.OPEN
    assert store.get_holding("OFF2") is None


def test_simulated_buy_records_no_holding(store):
    # A simulated order may still resolve, but must never touch the live ledger.
    client = FakeClient()
    executor = make_live(client=client, store=store)
    [buy] = [o for o in executor.execute([make_buy(nft="SIM1", price_usd=10,
                                                   market_usd=20, cycle_id="c",
                                                   simulated=True)])
             if o.kind is OrderKind.BUY]
    assert buy.status is OrderStatus.CONFIRMED
    assert store.get_holding("SIM1") is None


def test_relist_going_live_marks_holding_listed(store):
    client = FakeClient()
    executor = make_live(client=client, store=store)
    executor.execute([make_buy(nft="REL1", price_usd=10, market_usd=20,
                               cycle_id="c")])
    out = executor.relist(make_list(nft="REL1", price_usd=18, cycle_id="c"))
    assert out.status is OrderStatus.CONFIRMED
    holding = store.get_holding("REL1")
    assert holding.status == HOLDING_LISTED
    assert holding.listed_at > 0
    assert holding.list_price_usd == 18


def test_relist_without_prior_holding_writes_nothing(store):
    client = FakeClient()
    executor = make_live(client=client, store=store)
    executor.relist(make_list(nft="NOHOLD", price_usd=18, cycle_id="c"))
    assert store.get_holding("NOHOLD") is None


def test_holdings_write_failure_does_not_abort_buy(store, monkeypatch):
    client = FakeClient()
    executor = make_live(client=client, store=store)

    def boom(_holding):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "upsert_holding", boom)
    [buy] = [o for o in executor.execute([make_buy(nft="B1", price_usd=10,
                                                   market_usd=20, cycle_id="c")])
             if o.kind is OrderKind.BUY]
    assert buy.status is OrderStatus.CONFIRMED  # the order still settled


# --------------------------------------------------------------------------- #
# Authoritative sold-signal writer (ETAPPE 8.2)
# --------------------------------------------------------------------------- #
def test_record_sold_holding_marks_sold(store):
    store.upsert_holding(Holding(nft="S1", name="Card", category="Pokemon",
                                 acquired_at=1.0, cost_usd=10.0,
                                 market_usd_at_buy=20.0, status=HOLDING_HELD))
    holding = store.get_holding("S1")
    assert record_sold_holding(store, holding, now=123.0) is True
    after = store.get_holding("S1")
    assert after.status == HOLDING_SOLD
    assert after.sold_at == 123.0


def test_record_sold_holding_skips_already_sold(store):
    store.upsert_holding(Holding(nft="S2", name="Card", category="Pokemon",
                                 acquired_at=1.0, cost_usd=10.0,
                                 market_usd_at_buy=20.0, status=HOLDING_SOLD,
                                 sold_at=50.0))
    holding = store.get_holding("S2")
    assert record_sold_holding(store, holding, now=999.0) is False
    assert store.get_holding("S2").sold_at == 50.0  # unchanged


def test_record_sold_holding_guards_none():
    assert record_sold_holding(None, None) is False


# --------------------------------------------------------------------------- #
# Maintenance (ETAPPE 6/8)
# --------------------------------------------------------------------------- #
# DryRunExecutor simulates the transitions. LiveExecutor offer bump/cancel are
# LIVE (ETAPPE 8, verified update-offer / cancel-offer shapes); listing markdown
# and offer accept stay safe-failure (shapes still ASSUMED).
def _open_offer(nft="N", price=8.0, market=20.0, bump_count=0):
    o = make_offer(nft=nft, price_usd=price, market_usd=market, cycle_id="c")
    o.bump_count = bump_count
    o.transition(OrderStatus.OPEN)
    return o


def test_dryrun_bump_offer_raises_price_and_count():
    o = _open_offer(price=8.0, bump_count=1)
    out = DryRunExecutor().bump_offer(o, 8.10, now=1000.0)
    assert out.price_usd == 8.10
    assert out.bump_count == 2
    assert out.last_bump_at == 1000.0
    assert out.status is OrderStatus.OPEN


def test_dryrun_cancel_offer_cancels():
    o = _open_offer()
    out = DryRunExecutor().cancel_offer(o)
    assert out.status is OrderStatus.CANCELLED


def test_dryrun_markdown_listing_lowers_price():
    lst = make_list(nft="N", price_usd=25.0, cycle_id="c", simulated=True)
    out = DryRunExecutor().markdown_listing(lst, 23.0)
    assert out.price_usd == 23.0
    assert out.status is OrderStatus.PLANNED  # price edit, no state change


def test_dryrun_accept_offer_confirms():
    lst = make_list(nft="N", price_usd=25.0, cycle_id="c", simulated=True)
    out = DryRunExecutor().accept_offer(lst, "OFFER1")
    assert out.status is OrderStatus.CONFIRMED


# -- live offer bump (verified update-offer) -------------------------------- #
def test_live_bump_offer_raises_price_and_stays_open():
    client = FakeClient()
    executor = make_live(client=client)
    o = _open_offer(price=8.0, bump_count=1)
    out = executor.bump_offer(o, 8.10, now=1000.0)
    assert out.status is OrderStatus.OPEN     # never transitions OPEN->SIGNED
    assert out.price_usd == 8.10
    assert out.bump_count == 2
    assert out.last_bump_at == 1000.0


def test_live_bump_offer_calls_update_then_broadcast():
    client = FakeClient()
    executor = make_live(client=client)
    o = _open_offer(price=8.0)
    executor.bump_offer(o, 8.10)
    assert client.call_names() == ["update_offer", "broadcast"]


def test_live_bump_offer_update_body_has_wallet_and_price():
    client = FakeClient()
    wallet = FakeWallet(can_sign=True)
    executor = make_live(client=client, wallet=wallet)
    o = _open_offer(nft="NFT5", price=8.0)
    executor.bump_offer(o, 9.25)
    call = next(kw for name, kw in client.calls if name == "update_offer")
    assert call["nft"] == "NFT5"
    assert call["price"] == 9.25
    assert call["wallet"] == wallet.address


def test_live_bump_offer_records_signature():
    client = FakeClient()
    client.broadcast_response = {"success": True, "signature": "BUMPSIG"}
    executor = make_live(client=client)
    o = _open_offer(price=8.0)
    out = executor.bump_offer(o, 8.10)
    assert out.signature == "BUMPSIG"


def test_live_bump_offer_no_tx_leaves_offer_unchanged():
    client = FakeClient()
    client.responses["update_offer"] = {"nope": True}  # no transaction
    executor = make_live(client=client)
    o = _open_offer(price=8.0, bump_count=2)
    out = executor.bump_offer(o, 8.10)
    assert out.status is OrderStatus.OPEN
    assert out.price_usd == 8.0               # not bumped
    assert out.bump_count == 2
    assert client.count("broadcast") == 0


def test_live_bump_offer_broadcast_error_leaves_offer_open_no_retry():
    client = FakeClient()
    client.errors["broadcast"] = CCServerError("5xx")
    executor = make_live(client=client)
    o = _open_offer(price=8.0)
    out = executor.bump_offer(o, 8.10)
    assert out.status is OrderStatus.OPEN     # still resting at the old price
    assert out.price_usd == 8.0
    assert client.count("broadcast") == 1     # never retried


def test_live_bump_offer_sign_failure_leaves_offer_open():
    client = FakeClient()
    executor = make_live(client=client,
                         wallet=FakeWallet(can_sign=True, sign_error=True))
    o = _open_offer(price=8.0)
    out = executor.bump_offer(o, 8.10)
    assert out.status is OrderStatus.OPEN
    assert out.price_usd == 8.0
    assert client.count("broadcast") == 0


# -- live offer cancel (verified cancel-offer) ------------------------------ #
def test_live_cancel_offer_cancels():
    client = FakeClient()
    executor = make_live(client=client)
    o = _open_offer()
    out = executor.cancel_offer(o)
    assert out.status is OrderStatus.CANCELLED
    assert client.call_names() == ["cancel_offer", "broadcast"]


def test_live_cancel_offer_body_has_nft_and_wallet():
    client = FakeClient()
    wallet = FakeWallet(can_sign=True)
    executor = make_live(client=client, wallet=wallet)
    o = _open_offer(nft="NFT7")
    executor.cancel_offer(o)
    call = next(kw for name, kw in client.calls if name == "cancel_offer")
    assert call["nft"] == "NFT7"
    assert call["wallet"] == wallet.address


def test_live_cancel_offer_broadcast_error_leaves_offer_open_no_retry():
    client = FakeClient()
    client.errors["broadcast"] = CCServerError("5xx")
    executor = make_live(client=client)
    o = _open_offer()
    out = executor.cancel_offer(o)
    assert out.status is OrderStatus.OPEN     # still resting, escrow intact
    assert client.count("broadcast") == 1     # never retried


def test_live_cancel_offer_no_tx_leaves_offer_open():
    client = FakeClient()
    client.responses["cancel_offer"] = {"nope": True}  # no transaction
    executor = make_live(client=client)
    o = _open_offer()
    out = executor.cancel_offer(o)
    assert out.status is OrderStatus.OPEN
    assert client.count("broadcast") == 0


def test_live_markdown_listing_is_safe_noop():
    client = FakeClient()
    executor = make_live(client=client)
    lst = make_list(nft="N", price_usd=25.0, cycle_id="c")
    out = executor.markdown_listing(lst, 23.0)
    assert out.price_usd == 25.0              # unchanged
    assert client.calls == []


def test_live_accept_offer_is_safe_noop():
    client = FakeClient()
    executor = make_live(client=client)
    lst = make_list(nft="N", price_usd=25.0, cycle_id="c")
    out = executor.accept_offer(lst, "OFFER1")
    assert out.status is OrderStatus.PLANNED  # untouched
    assert client.calls == []


def test_live_maintenance_moves_no_money():
    client = FakeClient()
    executor = make_live(client=client, volume=100.0)
    executor.bump_offer(_open_offer(price=8.0), 8.10)
    executor.accept_offer(make_list(nft="N", price_usd=25.0, cycle_id="c"), "O")
    assert executor._remaining == 100.0       # budget never touched


# --------------------------------------------------------------------------- #
# DryRunExecutor (no side effects)
# --------------------------------------------------------------------------- #
def test_dryrun_buy_confirms_and_relists():
    result = DryRunExecutor().execute([make_buy(price_usd=10, market_usd=20,
                                                resell_usd=18, simulated=True)])
    kinds = sorted(o.kind.value for o in result)
    assert kinds == ["buy", "list"]
    assert all(o.status is OrderStatus.CONFIRMED for o in result)


def test_dryrun_buy_no_resell_no_relist():
    result = DryRunExecutor().execute([make_buy(price_usd=10, resell_usd=0,
                                                simulated=True)])
    assert all(o.kind is not OrderKind.LIST for o in result)


def test_dryrun_offer_opens():
    [offer] = DryRunExecutor().execute([make_offer(price_usd=8, simulated=True)])
    assert offer.status is OrderStatus.OPEN


def test_dryrun_list_confirms():
    [lst] = DryRunExecutor().execute([make_list(nft="N", price_usd=25,
                                               cycle_id="c", simulated=True)])
    assert lst.status is OrderStatus.CONFIRMED


def test_dryrun_touches_no_client_or_wallet():
    # DryRunExecutor has no client/wallet at all — proving zero side effects.
    result = DryRunExecutor().execute([make_buy(price_usd=10, market_usd=20)])
    assert result  # ran without any client/wallet dependency


# --------------------------------------------------------------------------- #
# Response-interpretation helpers (parametrized for breadth)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["transaction", "tx", "unsignedTransaction",
                                 "serializedTransaction", "txData",
                                 "encodedTransaction"])
def test_extract_tx_keys(key):
    assert _extract_tx({key: "TXDATA"}) == "TXDATA"


def test_extract_tx_envelope():
    assert _extract_tx({"data": {"transaction": "T"}}) == "T"


def test_extract_tx_missing():
    assert _extract_tx({"foo": "bar"}) == ""


@pytest.mark.parametrize("key", ["receiptId", "receipt_id", "id", "offerId",
                                 "offer_id", "listingId", "listing_id",
                                 "orderId"])
def test_extract_external_id_keys(key):
    assert _extract_external_id({key: "EXT"}) == "EXT"


@pytest.mark.parametrize("key", ["signature", "txSignature", "txid", "txId",
                                 "transactionSignature"])
def test_extract_signature_keys(key):
    assert _extract_signature({key: "SIG"}) == "SIG"


@pytest.mark.parametrize("status", ["confirmed", "finalized", "success",
                                    "succeeded", "ok", "complete", "completed",
                                    "CONFIRMED", "Success"])
def test_is_confirmed_status_values(status):
    assert _is_confirmed({"status": status})


@pytest.mark.parametrize("flag", ["confirmed", "success", "finalized"])
def test_is_confirmed_boolean_flags(flag):
    assert _is_confirmed({flag: True})


@pytest.mark.parametrize("status", ["pending", "open", "unknown", ""])
def test_is_confirmed_false(status):
    assert not _is_confirmed({"status": status})


@pytest.mark.parametrize("status", ["filled", "accepted", "sold", "matched",
                                    "FILLED"])
def test_is_filled_status_values(status):
    assert _is_filled({"status": status})


@pytest.mark.parametrize("status", ["cancelled", "canceled", "withdrawn",
                                    "expired", "rejected", "removed",
                                    "delisted"])
def test_is_cancelled_status_values(status):
    assert _is_cancelled({"status": status})


def test_is_cancelled_false_for_confirmed():
    assert not _is_cancelled({"status": "confirmed"})


def test_first_prefers_top_level():
    assert _first({"a": "x", "data": {"a": "y"}}, "a") == "x"


def test_first_falls_back_to_envelope():
    assert _first({"data": {"a": "y"}}, "a") == "y"


def test_first_ignores_empty_values():
    assert _first({"a": "", "b": "v"}, "a", "b") == "v"


def test_first_non_dict_returns_none():
    assert _first("notadict", "a") is None
