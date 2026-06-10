"""TradeEngine live-cycle wiring tests.

These verify the engine's *gating and orchestration* — the rules that decide
whether the live path is ever reached and that live-only maintenance runs only
when armed:

* the live executor is built ONLY when fully armed (live + can_sign + real auth);
* demo cycles are ALWAYS dry-run, even on a fully armed wallet;
* the config snapshot never leaks the wallet secret;
* status-sync + exit-flow run only on a live cycle with a store.

Sourcing and balance reads are faked so nothing hits the network.
"""
from __future__ import annotations

import pytest

from collectorcrypt.trader.engine import (TradeEngine, _config_snapshot,
                                          _order_states,
                                          _replay_wallet_activity)
from collectorcrypt.trader.executor import DryRunExecutor, LiveExecutor
from collectorcrypt.trader.orders import OrderKind, OrderStatus
from collectorcrypt.trader.store import Holding
from collectorcrypt.trader.strategy import BuyPlan, Candidate, Offer

from .conftest import (FakeClient, FakeSessionProvider, FakeWallet, make_buy,
                       make_config, make_list, make_offer, new_keypair,
                       keypair_secret)


class FakeSourceClient:
    """Minimal stand-in for the public CCClient used for sourcing."""

    def __init__(self, *, sol_rate=150.0, pages=None):
        self._sol_rate = sol_rate
        self._pages = pages or {1: {"filterNFtCard": [], "totalPages": 1}}

    def fetch_sol_usd(self):
        return self._sol_rate

    def fetch_marketplace_page_with_retry(self, page, step):
        return self._pages.get(page, {"filterNFtCard": [], "totalPages": 1})


def make_engine(cfg=None, *, wallet=None, store=None, provider=None,
                source=None):
    cfg = cfg or make_config()
    return TradeEngine(
        cfg,
        client=source or FakeSourceClient(),
        wallet=wallet or FakeWallet(can_sign=False),
        store=store,
        session_provider=provider or FakeSessionProvider(),
    )


@pytest.fixture(autouse=True)
def _no_real_trading_client(monkeypatch):
    """Default every engine-built trading client to a scriptable fake.

    The activity sync fetches the wallet feed on *every* live cycle, so a live
    test without an explicit client patch would otherwise hit the real API.
    Tests that need a configured fake simply re-patch inside their body.
    """
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: FakeClient())


def armed_cfg():
    # A kill switch of 3 also satisfies live_caps_configured(), so the R2 guard
    # (all risk limits = 0 → refuse to run) does not fire for these tests.
    return make_config(live=True, auth_provider="static", cc_token="tok",
                       max_consecutive_failures=3)


# --------------------------------------------------------------------------- #
# _is_live_armed matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("live,can_sign,auth,expected", [
    (True, True, "static", True),
    (True, True, "privy", True),
    (True, True, "none", False),   # no real auth provider
    (True, False, "static", False),  # cannot sign
    (False, True, "static", False),  # master switch off
    (False, False, "none", False),
])
def test_is_live_armed_matrix(live, can_sign, auth, expected):
    cfg = make_config(live=live, auth_provider=auth, cc_token="tok")
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=can_sign))
    assert engine._is_live_armed() is expected


# --------------------------------------------------------------------------- #
# _build_executor gating
# --------------------------------------------------------------------------- #
def test_build_executor_live_when_armed():
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True))
    assert isinstance(engine._build_executor(100.0), LiveExecutor)


def test_build_executor_dryrun_when_not_armed():
    engine = make_engine(make_config(live=False), wallet=FakeWallet(can_sign=True))
    assert isinstance(engine._build_executor(100.0), DryRunExecutor)


def test_build_executor_demo_always_dryrun_even_when_armed():
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True))
    assert isinstance(engine._build_executor(100.0, demo=True), DryRunExecutor)


def test_build_executor_dryrun_without_signing():
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=False))
    assert isinstance(engine._build_executor(100.0), DryRunExecutor)


def test_executor_property_reflects_gating():
    armed = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True))
    assert isinstance(armed.executor, LiveExecutor)
    unarmed = make_engine(make_config(live=False), wallet=FakeWallet(can_sign=True))
    assert isinstance(unarmed.executor, DryRunExecutor)


# --------------------------------------------------------------------------- #
# Config snapshot redaction
# --------------------------------------------------------------------------- #
def test_config_snapshot_omits_secret():
    secret = keypair_secret(new_keypair())
    cfg = make_config(wallet_secret=secret)
    snap = _config_snapshot(cfg)
    assert "wallet_secret" not in snap
    assert secret not in str(snap)


def test_config_snapshot_has_secret_flag_true():
    cfg = make_config(wallet_secret=keypair_secret(new_keypair()))
    assert _config_snapshot(cfg)["has_secret"] is True


def test_config_snapshot_has_secret_flag_false():
    cfg = make_config(wallet_secret="")
    assert _config_snapshot(cfg)["has_secret"] is False


@pytest.mark.parametrize("key", [
    "rpc_url", "wallet_address", "has_secret", "live", "min_card_usd",
    "max_spend_per_cycle_usd", "max_spend_per_day_usd", "max_open_positions",
    "max_consecutive_failures", "categories",
])
def test_config_snapshot_contains_key(key):
    assert key in _config_snapshot(make_config())


def test_config_snapshot_no_cc_token():
    cfg = make_config(cc_token="supersecrettoken")
    assert "supersecrettoken" not in str(_config_snapshot(cfg))


# --------------------------------------------------------------------------- #
# run_cycle: demo mode
# --------------------------------------------------------------------------- #
def test_demo_cycle_marked_demo():
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True))
    report = engine.run_cycle(sim_volume=500.0)
    assert report["demo"] is True
    assert report["mode"] == "DEMO"


def test_demo_cycle_does_not_read_wallet():
    # A wallet that raises if read proves demo never touches balances.
    class ExplodingWallet(FakeWallet):
        def sol_balance(self):
            raise AssertionError("wallet read in demo mode")

        def usdc_balance(self):
            raise AssertionError("wallet read in demo mode")

    engine = make_engine(armed_cfg(),
                         wallet=ExplodingWallet(can_sign=True))
    report = engine.run_cycle(sim_volume=500.0)  # must not raise
    assert report["available_volume"] == 500.0


def test_demo_cycle_uses_sim_volume():
    engine = make_engine(wallet=FakeWallet(can_sign=True))
    report = engine.run_cycle(sim_volume=777.0)
    assert report["available_volume"] == 777.0


def test_demo_cycle_no_status_sync(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle(sim_volume=500.0)
    assert "status_sync" not in report


def test_demo_cycle_not_persisted(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    engine.run_cycle(sim_volume=500.0)
    assert store.recent_cycles() == []


# --------------------------------------------------------------------------- #
# run_cycle: dry-run (not live)
# --------------------------------------------------------------------------- #
def test_dryrun_cycle_mode():
    engine = make_engine(make_config(live=False),
                        wallet=FakeWallet(can_sign=True))
    report = engine.run_cycle(sim_volume=None)
    assert report["mode"] == "DRY-RUN"
    assert report["demo"] is False


def test_dryrun_cycle_no_status_sync(store):
    engine = make_engine(make_config(live=False),
                        wallet=FakeWallet(can_sign=True), store=store)
    report = engine.run_cycle()
    assert "status_sync" not in report


# --------------------------------------------------------------------------- #
# run_cycle: live wiring (empty listings -> no network, but maintenance runs)
# --------------------------------------------------------------------------- #
def test_live_cycle_mode():
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=None)
    report = engine.run_cycle()
    assert report["mode"] == "LIVE"


def test_live_cycle_runs_status_sync(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "status_sync" in report
    assert report["status_sync"]["checked"] == 0  # nothing active


def test_live_cycle_runs_exit_flow(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "relisted" in report


def test_live_cycle_includes_risk_posture(store):
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      max_spend_per_cycle_usd=50)
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True), store=store)
    report = engine.run_cycle()
    assert "risk" in report
    assert report["risk"]["limits"]["max_spend_per_cycle_usd"] == 50


def test_live_cycle_persists(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    engine.run_cycle()
    assert len(store.recent_cycles()) == 1


def test_live_cycle_exit_flow_lists_relist_candidate(store):
    # Pre-seed a PLANNED relist candidate; the live exit flow should list it.
    store.upsert_order(make_list(nft="R", price_usd=25, cycle_id="prev"))
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert len(report["relisted"]) == 1
    assert report["relisted"][0]["nft"] == "R"


# --------------------------------------------------------------------------- #
# Maintenance passes (ETAPPE 6): bump / cancel / markdown / accept / recheck
# --------------------------------------------------------------------------- #
_MAINT_KEYS = ("bumped", "cancelled", "marked_down", "offers_accepted",
               "market_recheck", "activity_sync")


def _seed_aged_open_offer(store, *, nft="O1", price=8.0, bump_count=0,
                          age_sec=200_000.0):
    """Persist an OPEN offer old enough to be due for a bump."""
    import time as _t
    o = make_offer(nft=nft, price_usd=price, market_usd=20.0, cycle_id="prev")
    o.bump_count = bump_count
    o.transition(OrderStatus.OPEN)
    o.created_at = _t.time() - age_sec
    store.upsert_order(o)


def test_live_cycle_exposes_maintenance_keys(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    for key in _MAINT_KEYS:
        assert key in report


def test_live_cycle_market_recheck_is_readonly_stub(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    # With no held cards there is nothing to re-check.
    assert report["market_recheck"]["checked"] == 0
    assert report["market_recheck"]["raised"] == []


def test_dryrun_cycle_no_maintenance_keys(store):
    engine = make_engine(make_config(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    for key in _MAINT_KEYS:
        assert key not in report


def test_demo_cycle_no_maintenance_keys():
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=None)
    report = engine.run_cycle()
    for key in _MAINT_KEYS:
        assert key not in report


def test_live_cycle_bumps_due_offer(store, monkeypatch):
    # An aged OPEN offer is surfaced by the bump pass and bumped LIVE via the
    # verified update-offer endpoint (Etappe 8). A resting offer never passes
    # through SIGNED, so it stays OPEN at the higher price with bump_count++.
    fake = FakeClient()
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_aged_open_offer(store, nft="O1", price=8.0)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert len(report["bumped"]) == 1
    assert report["bumped"][0]["nft"] == "O1"
    assert report["bumped"][0]["bump_count"] == 1
    # The live bump went out (update-offer -> broadcast) and was persisted.
    assert "update_offer" in fake.call_names()
    persisted = store.open_offers()
    assert len(persisted) == 1
    assert persisted[0].status is OrderStatus.OPEN
    assert persisted[0].bump_count == 1
    # The persisted price is exactly the strategy's computed bump price.
    assert persisted[0].price_usd == report["bumped"][0]["new_price_usd"]


def test_live_cycle_markdown_persists_on_confirm(store, monkeypatch):
    # An aged, above-floor listing is marked down LIVE (update-listing) and the
    # new price + step counter are persisted on the holding so the curve moves.
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "M1"}], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    import time as _t
    old = _t.time() - 100 * 86400.0
    store.upsert_holding(Holding(
        nft="M1", name="Card", category="Pokemon", acquired_at=old,
        cost_usd=10.0, market_usd_at_buy=20.0, status="listed",
        list_price_usd=20.0, listed_at=old))
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert len(report["marked_down"]) == 1
    row = report["marked_down"][0]
    assert row["nft"] == "M1"
    assert row["status"] == "confirmed"
    assert "update_listing" in fake.call_names()
    held = store.get_holding("M1")
    assert held.list_price_usd == row["new_price_usd"]
    assert held.list_price_usd < 20.0          # stepped down
    assert held.markdown_steps == 1            # curve advanced
    assert held.last_markdown_at is not None


def test_halted_cycle_empties_send_passes_but_keeps_recheck(store):
    # Trip the kill switch with three real consecutive failures.
    for i in range(3):
        f = make_buy(nft=f"F{i}", cycle_id="prev")
        f.transition(OrderStatus.FAILED, detail="x")
        store.upsert_order(f)
    _seed_aged_open_offer(store, nft="O1", price=8.0)
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      max_consecutive_failures=3)
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True), store=store)
    report = engine.run_cycle()
    assert report["relisted"] == []
    assert report["bumped"] == []
    assert report["cancelled"] == []
    assert report["marked_down"] == []
    assert report["offers_accepted"] == []
    # The read-only re-check still runs while halted.
    assert report["market_recheck"]["checked"] == 0


# --------------------------------------------------------------------------- #
# Offer-accept pass (ETAPPE 8.3): card-activity feed -> accept best bid
# --------------------------------------------------------------------------- #
_ACCEPT_DAY = 86400.0


def _seed_floored_listing(store, *, nft="A1", cost=10.0, list_price=10.0):
    """Seed a holding listed at the cost floor and old enough to accept bids."""
    import time as _t
    old = _t.time() - 100 * _ACCEPT_DAY
    store.upsert_holding(Holding(
        nft=nft, name="Card", category="Pokemon", acquired_at=old,
        cost_usd=cost, market_usd_at_buy=20.0, status="listed",
        list_price_usd=list_price, listed_at=old))


def _accept_feed(client, nft, feed):
    """Make the FakeClient return *feed* for activity and keep *nft* owned."""
    client.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": nft}], "totalPages": 1}
    client.responses["get_card_activity"] = {"data": feed}


def _bid(wallet, amount, action="Offer Made"):
    return {"action": action, "from": {"wallet": wallet}, "amount": amount}


def test_accept_pass_accepts_best_active_offer(store, monkeypatch):
    fake = FakeClient()
    _accept_feed(fake, "A1", [_bid("WB", 12.0), _bid("WA", 18.0)])
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_floored_listing(store, nft="A1")
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert len(report["offers_accepted"]) == 1
    row = report["offers_accepted"][0]
    assert row["nft"] == "A1"
    assert row["buyer"] == "WA"
    assert row["offer_usd"] == 18.0
    assert row["status"] == "confirmed"
    # The live accept went out (accept-offer -> broadcast) with the best bid.
    call = next(kw for name, kw in fake.calls if name == "accept_offer")
    assert call["buyer"] == "WA"
    assert call["price"] == 18.0
    # The holding is now marked sold.
    assert store.get_holding("A1").status == "sold"
    assert store.get_holding("A1").sold_at is not None


def test_accept_pass_skips_when_no_active_offer(store, monkeypatch):
    fake = FakeClient()
    # Only a cancelled offer -> nothing active.
    _accept_feed(fake, "A1", [_bid("WA", 18.0, action="Offer Cancelled")])
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_floored_listing(store, nft="A1")
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["offers_accepted"][0]["status"] == "skipped"
    assert "accept_offer" not in fake.call_names()
    assert store.get_holding("A1").status == "listed"


def test_accept_pass_skips_offer_below_min_market(store, monkeypatch):
    fake = FakeClient()
    _accept_feed(fake, "A1", [_bid("WA", 5.0)])
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_floored_listing(store, nft="A1")
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      offer_accept_min_market_pct=80.0,  # 80% of 20 = 16
                      max_consecutive_failures=3)
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True), store=store)
    report = engine.run_cycle()
    assert report["offers_accepted"][0]["status"] == "skipped"
    assert "accept_offer" not in fake.call_names()
    assert store.get_holding("A1").status == "listed"


def test_accept_pass_read_error_is_safe(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "A1"}], "totalPages": 1}
    fake.errors["get_card_activity"] = RuntimeError("feed down")
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_floored_listing(store, nft="A1")
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "error" in report["offers_accepted"][0]
    assert "accept_offer" not in fake.call_names()
    assert store.get_holding("A1").status == "listed"


# --------------------------------------------------------------------------- #
# Activity sync: restart recovery from the wallet activity feed
# --------------------------------------------------------------------------- #
_OUR_WALLET = "WALLEThdrtest"  # FakeWallet default address


def _ev(action, nft, *, frm=None, to=None, amount=None, card_id="",
        name="", age_sec=60.0):
    """Build one wallet-activity feed event (shape per captured feed)."""
    from datetime import datetime, timezone, timedelta
    created = (datetime.now(timezone.utc)
               - timedelta(seconds=age_sec)).isoformat()
    card = ({"id": card_id, "itemName": name, "category": "Pokemon"}
            if card_id or name else None)
    return {
        "action": action,
        "amount": amount,
        "nftAddress": nft,
        "from": {"wallet": frm} if frm else None,
        "to": {"wallet": to} if to else None,
        "card": card,
        "createdAt": created,
    }


def test_replay_sale_to_us_is_buy():
    state = _replay_wallet_activity(
        [_ev("Sale", "N1", frm="SELLER", to=_OUR_WALLET, amount=95.06,
             card_id="C1", name="Charizard")], _OUR_WALLET)
    assert state.buys["N1"]["price"] == 95.06
    assert state.buys["N1"]["name"] == "Charizard"
    assert "N1" not in state.exits


def test_replay_sale_from_us_is_exit():
    # Feed is newest-first: the sale happened AFTER our listing.
    state = _replay_wallet_activity([
        _ev("Sale", "N1", frm=_OUR_WALLET, to="BUYER", amount=50.0,
            age_sec=10),
        _ev("List", "N1", frm=_OUR_WALLET, amount=55.0, age_sec=100),
    ], _OUR_WALLET)
    assert "N1" in state.exits
    assert "N1" not in state.listings


def test_replay_offer_lifecycle_last_event_wins():
    # made -> updated -> cancelled (newest first in the feed)
    state = _replay_wallet_activity([
        _ev("Offer Cancelled", "N1", frm=_OUR_WALLET, age_sec=10),
        _ev("Offer Updated", "N1", frm=_OUR_WALLET, amount=22.0, age_sec=20),
        _ev("Offer Made", "N1", frm=_OUR_WALLET, amount=20.0, age_sec=30),
    ], _OUR_WALLET)
    assert "N1" not in state.open_offers
    # Without the cancel, the update supersedes the original price.
    state = _replay_wallet_activity([
        _ev("Offer Updated", "N1", frm=_OUR_WALLET, amount=22.0, age_sec=20),
        _ev("Offer Made", "N1", frm=_OUR_WALLET, amount=20.0, age_sec=30),
    ], _OUR_WALLET)
    assert state.open_offers["N1"]["price"] == 22.0


def test_replay_offer_accepted_to_us_is_buy_and_closes_offer():
    # Our standing offer filled: the seller accepted -> we bought at amount.
    state = _replay_wallet_activity([
        _ev("Offer Accepted", "N1", frm="SELLER", to=_OUR_WALLET,
            amount=38.18, age_sec=10),
        _ev("Offer Made", "N1", frm=_OUR_WALLET, amount=38.18, age_sec=20),
    ], _OUR_WALLET)
    assert "N1" not in state.open_offers
    assert state.buys["N1"]["price"] == 38.18


def test_replay_offer_accepted_from_us_is_exit():
    # We accepted an incoming offer on our card -> we sold (net proceeds).
    state = _replay_wallet_activity(
        [_ev("Offer Accepted", "N1", frm=_OUR_WALLET, to="BIDDER",
             amount=15.5232)], _OUR_WALLET)
    assert "N1" in state.exits
    assert "N1" not in state.buys


def test_replay_list_then_unlist():
    state = _replay_wallet_activity([
        _ev("Unlisted", "N1", frm=_OUR_WALLET, age_sec=10),
        _ev("List", "N1", frm=_OUR_WALLET, amount=49.75, age_sec=20),
    ], _OUR_WALLET)
    assert "N1" not in state.listings
    state = _replay_wallet_activity([
        _ev("Listing Updated", "N1", frm=_OUR_WALLET, amount=48.5, age_sec=10),
        _ev("List", "N1", frm=_OUR_WALLET, amount=49.75, age_sec=20),
    ], _OUR_WALLET)
    assert state.listings["N1"]["price"] == 48.5


def test_replay_ignores_foreign_events():
    state = _replay_wallet_activity([
        _ev("Offer Made", "N1", frm="SOMEONE", amount=25.49),
        _ev("Sale", "N2", frm="A", to="B", amount=10.0),
        _ev("List", "N3", frm="C", amount=5.0),
    ], _OUR_WALLET)
    assert not state.buys and not state.open_offers and not state.listings


def test_live_cycle_exposes_activity_sync_key(store):
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["activity_sync"]["checked"] == 0
    assert report["activity_sync"]["recovered_offers"] == []


def test_activity_sync_recovers_open_offer(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_wallet_activity"] = {"data": [
        _ev("Offer Made", "OFR1", frm=_OUR_WALLET, amount=20.0,
            card_id="CARD1", name="Pikachu"),
    ]}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert [r["nft"] for r in report["activity_sync"]["recovered_offers"]] == ["OFR1"]
    offers = store.open_offers()
    assert len(offers) == 1
    assert offers[0].nft == "OFR1"
    assert offers[0].status is OrderStatus.OPEN
    assert offers[0].price_usd == 20.0
    assert offers[0].card_id == "CARD1"
    assert offers[0].simulated is False


def test_activity_sync_skips_cancelled_offer(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_wallet_activity"] = {"data": [
        _ev("Offer Cancelled", "OFR1", frm=_OUR_WALLET, age_sec=10),
        _ev("Offer Made", "OFR1", frm=_OUR_WALLET, amount=20.0, age_sec=20),
    ]}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["activity_sync"]["recovered_offers"] == []
    assert store.open_offers() == []


def test_activity_sync_does_not_duplicate_known_offer(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_wallet_activity"] = {"data": [
        _ev("Offer Made", "O1", frm=_OUR_WALLET, amount=8.0),
    ]}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    o = make_offer(nft="O1", price_usd=8.0, market_usd=20.0, cycle_id="prev")
    o.transition(OrderStatus.OPEN)
    store.upsert_order(o)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["activity_sync"]["recovered_offers"] == []
    assert len(store.open_offers()) == 1


def test_activity_sync_recreates_missing_holding(store, monkeypatch):
    # Fresh DB after a restart: a bought-and-listed card is rebuilt entirely
    # from the feed — with cost basis and listing state.
    fake = FakeClient()
    fake.responses["get_wallet_activity"] = {"data": [
        _ev("List", "H1", frm=_OUR_WALLET, amount=120.0, card_id="C9",
            name="Mewtwo", age_sec=10),
        _ev("Sale", "H1", frm="SELLER", to=_OUR_WALLET, amount=95.06,
            card_id="C9", name="Mewtwo", age_sec=20),
    ]}
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "H1"}], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert [r["nft"] for r in report["activity_sync"]["recovered_holdings"]] == ["H1"]
    h = store.get_holding("H1")
    assert h.cost_usd == 95.06
    assert h.name == "Mewtwo"
    assert h.status == "listed"
    assert h.list_price_usd == 120.0
    assert h.listed_at is not None


def test_activity_sync_skips_card_sold_within_feed(store, monkeypatch):
    # Bought and later sold inside the feed window -> nothing to recreate.
    fake = FakeClient()
    fake.responses["get_wallet_activity"] = {"data": [
        _ev("Sale", "H1", frm=_OUR_WALLET, to="BUYER", amount=120.0,
            age_sec=10),
        _ev("Sale", "H1", frm="SELLER", to=_OUR_WALLET, amount=95.0,
            age_sec=20),
    ]}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["activity_sync"]["recovered_holdings"] == []
    assert store.get_holding("H1") is None


def test_activity_sync_backfills_zero_cost(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_wallet_activity"] = {"data": [
        _ev("Sale", "H1", frm="SELLER", to=_OUR_WALLET, amount=42.5),
    ]}
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "H1"}], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    store.upsert_holding(Holding(nft="H1", name="Card", acquired_at=1.0,
                                 cost_usd=0.0, market_usd_at_buy=0.0,
                                 status="held"))
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["activity_sync"]["backfilled"] == [
        {"nft": "H1", "fields": ["cost_usd", "market_usd_at_buy"]}]
    assert store.get_holding("H1").cost_usd == 42.5
    assert store.get_holding("H1").market_usd_at_buy == 42.5


def test_activity_sync_never_overwrites_existing_cost(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_wallet_activity"] = {"data": [
        _ev("Sale", "H1", frm="SELLER", to=_OUR_WALLET, amount=42.5),
    ]}
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "H1"}], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="H1")  # cost_usd=10.0 already known
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["activity_sync"]["backfilled"] == []
    assert store.get_holding("H1").cost_usd == 10.0


def test_activity_sync_fetch_error_recovers_nothing(store, monkeypatch):
    from collectorcrypt.trader.ccapi import CCServerError
    fake = FakeClient()
    fake.errors["get_wallet_activity"] = CCServerError("api down", status=503)
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "error" in report["activity_sync"]
    assert store.open_offers() == []


# --------------------------------------------------------------------------- #
# Ownership sync (ETAPPE 8.2): authoritative sold-signal via owned-cards
# --------------------------------------------------------------------------- #
def _seed_held(store, *, nft, name="Card"):
    store.upsert_holding(Holding(nft=nft, name=name, category="Pokemon",
                                 acquired_at=1.0, cost_usd=10.0,
                                 market_usd_at_buy=20.0, status="held"))


def test_live_cycle_exposes_ownership_sync_key(store, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "ownership_sync" in report


def test_ownership_sync_marks_absent_holding_sold(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {"filterNFtCard": [], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="GONE")
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["ownership_sync"]["checked"] == 1
    assert [s["nft"] for s in report["ownership_sync"]["sold"]] == ["GONE"]
    assert store.get_holding("GONE").status == "sold"
    assert store.get_holding("GONE").sold_at is not None


def test_ownership_sync_records_sold_to_ledger(store, monkeypatch, tmp_path):
    import csv

    ledger_path = tmp_path / "records" / "transactions.csv"
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {"filterNFtCard": [], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="GONE", name="Blastoise")
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      ledger_path=str(ledger_path))
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True), store=store)
    engine.run_cycle()
    with open(ledger_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["event"] for r in rows] == ["sold"]
    assert rows[0]["nft_address"] == "GONE"
    assert rows[0]["card_name"] == "Blastoise"


def test_ownership_sync_keeps_owned_holding(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "KEEP"}], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="KEEP")
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["ownership_sync"]["sold"] == []
    assert store.get_holding("KEEP").status == "held"
    assert store.get_holding("KEEP").sold_at is None


def test_ownership_sync_fetch_error_marks_nothing_sold(store, monkeypatch):
    fake = FakeClient()
    fake.errors["get_owned_cards"] = RuntimeError("api down")
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="SAFE")
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "error" in report["ownership_sync"]
    # Fail-safe: an unreadable owned set never marks a card sold.
    assert store.get_holding("SAFE").status == "held"
    assert store.get_holding("SAFE").sold_at is None


def test_fetch_owned_nfts_paginates_all_pages(store, monkeypatch):
    class _PagedClient:
        def __init__(self):
            self.pages = {
                1: {"filterNFtCard": [{"nftAddress": "A"}], "totalPages": 2},
                2: {"filterNFtCard": [{"nftAddress": "B"}], "totalPages": 2},
            }
            self.calls = 0

        def get_owned_cards(self, *, wallet, page=1, step=96,
                            order_by="dateDesc"):
            self.calls += 1
            return self.pages[page]

    paged = _PagedClient()
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: paged)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    owned = engine._fetch_owned_nfts()
    assert owned == {"A", "B"}
    assert paged.calls == 2  # both pages fetched, page-2 card not lost


# --------------------------------------------------------------------------- #
# Market-value re-check (ETAPPE 8.4): oraclePrice from owned-cards (feature 5b)
# --------------------------------------------------------------------------- #
def _owned_card(nft, price):
    return {"nftAddress": nft, "oraclePrice": price}


def test_market_recheck_raises_on_positive_market(store, monkeypatch):
    fake = FakeClient()
    # Card still owned (so ownership_sync keeps it) but now worth more.
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [_owned_card("R1", "30")], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="R1")  # market_usd_at_buy=20.0
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["market_recheck"]["checked"] == 1
    assert [r["nft"] for r in report["market_recheck"]["raised"]] == ["R1"]
    held = store.get_holding("R1")
    assert held.market_usd_current == 30.0
    assert held.market_usd_at_buy == 30.0  # new reference persisted
    assert held.market_checked_at is not None
    assert held.markdown_steps == 0  # sell cycle restarted at day 0
    assert held.last_markdown_at is None


def test_market_recheck_flat_records_value_without_raise(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [_owned_card("R2", "15")], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="R2")  # market_usd_at_buy=20.0
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["market_recheck"]["checked"] == 1
    assert report["market_recheck"]["raised"] == []
    held = store.get_holding("R2")
    assert held.market_usd_current == 15.0
    assert held.market_usd_at_buy == 20.0  # unchanged on a flat/negative move
    assert held.market_checked_at is not None


def test_market_recheck_fetch_error_is_failsafe(store, monkeypatch):
    fake = FakeClient()
    fake.errors["get_owned_cards"] = RuntimeError("api down")
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_held(store, nft="R3")
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "error" in report["market_recheck"]
    held = store.get_holding("R3")
    assert held.market_checked_at is None  # nothing re-checked on a fetch error


def test_market_recheck_skips_when_not_due(store, monkeypatch):
    import time

    fake = FakeClient()
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [_owned_card("R4", "99")], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    # Re-checked just now -> not yet due (default market_recheck_hours=24).
    store.upsert_holding(Holding(nft="R4", name="Card", category="Pokemon",
                                 acquired_at=1.0, cost_usd=10.0,
                                 market_usd_at_buy=20.0,
                                 market_checked_at=time.time(), status="held"))
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["market_recheck"]["checked"] == 0
    assert report["market_recheck"]["raised"] == []
    assert store.get_holding("R4").market_usd_current is None


def _raw_card(nft, *, price="10", insured=100, category="Pokemon",
              marketplace="CC"):
    """A raw CC API card that normalizes into a qualifying listing."""
    return {
        "itemName": f"Card {nft}",
        "nftAddress": nft,
        "category": category,
        "insuredValue": insured,
        "listing": {"price": price, "currency": "USDC",
                    "marketplace": marketplace},
    }


def _source_with(*cards):
    return FakeSourceClient(pages={1: {"filterNFtCard": list(cards),
                                       "totalPages": 1}})


class _LowVolumeWallet(FakeWallet):
    """A signing wallet whose USDC balance sits below the min-operate floor."""

    def usdc_balance(self) -> float:
        return 50.0


def test_min_operate_gate_pauses_acquisition():
    cfg = make_config(min_operate_usd=100.0)  # dry-run
    engine = make_engine(cfg, wallet=_LowVolumeWallet(can_sign=True),
                        source=_source_with(_raw_card("A")))
    report = engine.run_cycle()  # available 50 < min 100
    assert report["acquisition_paused"] is True
    assert "paused" in report["pause_reason"]
    assert report["scanned"] == 0       # sourcing skipped entirely
    assert report["candidates"] == 0
    assert report["planned_buys"] == 0
    assert report["planned_offers"] == 0


def test_min_operate_gate_allows_when_above_floor():
    cfg = make_config(min_operate_usd=10.0)  # 1000 available >= 10
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True),
                        source=_source_with(_raw_card("A")))
    report = engine.run_cycle()
    assert "acquisition_paused" not in report
    assert report["candidates"] == 1


def test_min_operate_gate_disabled_when_zero():
    cfg = make_config(min_operate_usd=0.0)
    engine = make_engine(cfg, wallet=_LowVolumeWallet(can_sign=True),
                        source=_source_with(_raw_card("A")))
    report = engine.run_cycle()  # 50 available, but gate off
    assert "acquisition_paused" not in report
    assert report["candidates"] == 1


def test_min_operate_gate_exempts_demo():
    cfg = make_config(min_operate_usd=1000.0)
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True),
                        source=_source_with(_raw_card("A")))
    report = engine.run_cycle(sim_volume=5.0)  # tiny demo volume
    assert "acquisition_paused" not in report  # demo never pauses


def test_min_operate_pause_keeps_maintenance(store):
    # A paused live cycle still reconciles in-flight orders (read-only).
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      min_operate_usd=100.0)
    engine = make_engine(cfg, wallet=_LowVolumeWallet(can_sign=True),
                        store=store, source=_source_with(_raw_card("A")))
    report = engine.run_cycle()
    assert report["acquisition_paused"] is True
    assert "status_sync" in report      # maintenance still runs
    assert "relisted" in report


def test_blacklist_filters_sourcing(store):
    # Seed a held, blacklisted holding; its NFT must vanish from sourcing.
    store.upsert_holding(Holding(nft="A", name="Card A", category="Pokemon",
                                 acquired_at=1.0, cost_usd=5.0,
                                 market_usd_at_buy=100.0, status="held"))
    store.mark_blacklisted("A")
    engine = make_engine(make_config(), wallet=FakeWallet(can_sign=True),
                        store=store,
                        source=_source_with(_raw_card("A"), _raw_card("B")))
    report = engine.run_cycle()
    nfts = {c["nft"] for c in report["items"]}
    assert "A" not in nfts          # blacklisted dropped from buys
    assert "B" in nfts              # other card still sourced
    # Blacklist also covers the offer pool, not just direct buys.
    assert "A" not in {o["nft"] for o in report["offers"]}


def test_blacklist_empty_when_no_store():
    # No store -> empty blacklist -> nothing filtered (best-effort).
    engine = make_engine(make_config(), wallet=FakeWallet(can_sign=True),
                        source=_source_with(_raw_card("A")))
    report = engine.run_cycle()
    assert report["candidates"] == 1


def test_max_owned_cap_blocks_surplus_buy_live(store):
    # One confirmed buy already owned; cap of 1 blocks the new buy.
    store.upsert_holding(Holding(nft="OWNED", name="Owned", category="Pokemon",
                                 acquired_at=1.0, cost_usd=5.0,
                                 market_usd_at_buy=100.0, status="held"))
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      max_owned_cards=1, direct_buy_pct=100.0, offer_pct=0.0)
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True), store=store,
                        source=_source_with(_raw_card("NEW")))
    report = engine.run_cycle()
    assert report["risk"]["limits"]["max_owned_cards"] == 1
    assert report["risk"]["usage"]["owned_cards"] == 1
    # The new buy is risk-blocked (failed), not executed.
    new = [o for o in report["executed"] if o["nft"] == "NEW"]
    assert new and new[0]["status"] == "failed"
    assert "max owned cards" in new[0]["detail"]


# --------------------------------------------------------------------------- #
# _order_states helper
# --------------------------------------------------------------------------- #
def test_order_states_counts():
    a = make_buy(nft="A")
    b = make_buy(nft="B")
    b.transition(OrderStatus.CONFIRMED)
    counts = _order_states([a, b])
    assert counts["planned"] == 1
    assert counts["confirmed"] == 1


def test_order_states_empty():
    assert _order_states([]) == {}


# --------------------------------------------------------------------------- #
# R2 guard: refuse live trading when all risk limits are 0
# --------------------------------------------------------------------------- #
def test_live_cycle_refused_when_all_risk_limits_zero(store, monkeypatch):
    # An armed live cycle with every risk cap at 0 must block all orders and
    # surface a halted posture — the bot must not run uncapped with real funds.
    fake = FakeClient()
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    # all caps = 0 (the default make_config has no risk limits set)
    cfg = make_config(live=True, auth_provider="static", cc_token="tok")
    source = _source_with(_raw_card("N1", price="10", insured=200))
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True),
                         store=store, source=source)
    report = engine.run_cycle()
    # Risk guard fires: posture reports halted
    assert report["risk"]["halted"] is True
    assert "risk limits" in report["risk"]["halt_reason"].lower()
    # No live orders were sent despite there being a candidate. The activity
    # sync's read-only feed fetch is allowed (recovery runs even while
    # halted, like ownership sync); anything state-changing is not.
    assert fake.call_names() in ([], ["get_wallet_activity"])


# --------------------------------------------------------------------------- #
# Dynamic range bidding (escrow-leak fix): reprice offers vs the order book
# --------------------------------------------------------------------------- #
def _dyn_cfg(**kw):
    base = dict(live=True, auth_provider="static", cc_token="tok",
                max_consecutive_failures=3, offer_open_discount_pct=30.0,
                offer_ceiling_pct=10.0, offer_increment_usd=0.01)
    base.update(kw)
    return make_config(**base)


def _offer_plan(*, ask=100.0, resell=110.0, static_bid=75.0, budget=1000.0,
                cap=1000.0, nft="X1"):
    # ask=100 -> open_price=70 (30% off), ceiling_price=90 (10% off).
    cand = Candidate(card={"nft": nft, "name": "C"}, ask_usd=ask,
                     market_usd=ask * 1.2, discount_pct=0.0, resell_usd=resell)
    return BuyPlan(offers=[Offer(cand, static_bid)], offer_budget=budget,
                   card_cap_usd=cap)


def test_reprice_disabled_returns_none_and_keeps_static_bid():
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True))
    plan = _offer_plan(static_bid=75.0)
    assert engine._reprice_offers_dynamically(plan) is None
    assert plan.offers[0].offer_usd == 75.0  # untouched when feature is off


def test_reprice_uncontested_uses_open_price(monkeypatch):
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", lambda nft: [])
    plan = _offer_plan()
    report = engine._reprice_offers_dynamically(plan)
    assert report is not None
    assert len(plan.offers) == 1
    assert plan.offers[0].offer_usd == 70.0
    assert report[0]["status"] == "repriced"
    assert report[0]["offer_usd"] == 70.0


def test_reprice_outbids_competitor_in_range(monkeypatch):
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity",
                        lambda nft: [_bid("WB", 85.0)])
    plan = _offer_plan()
    engine._reprice_offers_dynamically(plan)
    assert plan.offers[0].offer_usd == 85.01  # outbid by the increment


def test_reprice_drops_offer_above_ceiling(monkeypatch):
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity",
                        lambda nft: [_bid("WB", 95.0)])  # above ceiling 90
    plan = _offer_plan()
    report = engine._reprice_offers_dynamically(plan)
    assert plan.offers == []                       # offer dropped
    assert report[0]["status"] == "skipped"


def test_reprice_excludes_our_own_bid(monkeypatch):
    # Our own bid is the highest, but it must be ignored so we don't outbid
    # ourselves; the real competitor sits below the open price -> bid open.
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(
        engine, "_fetch_card_activity",
        lambda nft: [_bid("WALLEThdrtest", 99.0), _bid("WB", 50.0)])
    plan = _offer_plan()
    engine._reprice_offers_dynamically(plan)
    assert plan.offers[0].offer_usd == 70.0


def test_reprice_skips_when_budget_below_open(monkeypatch):
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", lambda nft: [])
    plan = _offer_plan(budget=60.0)  # cannot even cover the open price (70)
    report = engine._reprice_offers_dynamically(plan)
    assert plan.offers == []
    assert report[0]["status"] == "skipped"


def test_reprice_skips_when_open_breaks_resale_floor(monkeypatch):
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", lambda nft: [])
    plan = _offer_plan(resell=68.0)  # open 70 >= resell 68 -> no profit -> skip
    report = engine._reprice_offers_dynamically(plan)
    assert plan.offers == []
    assert report[0]["status"] == "skipped"


def test_reprice_budget_shared_cheapest_first(monkeypatch):
    # Two offers, budget only large enough for the cheaper card's open price.
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", lambda nft: [])
    cheap = Offer(Candidate(card={"nft": "CHEAP", "name": "c"}, ask_usd=100.0,
                            market_usd=120.0, discount_pct=0.0, resell_usd=110.0),
                  75.0)   # open 70
    dear = Offer(Candidate(card={"nft": "DEAR", "name": "d"}, ask_usd=200.0,
                           market_usd=240.0, discount_pct=0.0, resell_usd=220.0),
                 150.0)   # open 140
    plan = BuyPlan(offers=[dear, cheap], offer_budget=100.0, card_cap_usd=1000.0)
    engine._reprice_offers_dynamically(plan)
    # Cheapest is funded first (70); the dear card no longer fits (140 > 30 left).
    kept = {o.candidate.nft: o.offer_usd for o in plan.offers}
    assert kept == {"CHEAP": 70.0}


def test_reprice_read_error_falls_back_to_static_bid(monkeypatch):
    def boom(nft):
        raise RuntimeError("order book down")

    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", boom)
    plan = _offer_plan(static_bid=60.0)  # fits budget + below resale -> kept
    report = engine._reprice_offers_dynamically(plan)
    assert plan.offers[0].offer_usd == 60.0
    assert report[0]["status"] == "fallback"


def test_reprice_read_error_drops_when_static_bid_unaffordable(monkeypatch):
    def boom(nft):
        raise RuntimeError("order book down")

    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", boom)
    plan = _offer_plan(static_bid=200.0, resell=110.0)  # static bid > resale
    report = engine._reprice_offers_dynamically(plan)
    assert plan.offers == []
    assert report[0]["status"] == "skipped"


def test_reprice_simulation_uses_open_price_without_reading_book(monkeypatch):
    # Dry-run/demo cannot read the live order book; with read_book=False the
    # card is assumed uncontested and quoted at the opening lowball. The feed
    # is never fetched (the stub would raise if it were).
    def boom(nft):
        raise AssertionError("order book must not be read in simulation")

    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", boom)
    plan = _offer_plan(static_bid=75.0)
    report = engine._reprice_offers_dynamically(plan, read_book=False)
    assert plan.offers[0].offer_usd == 70.0          # open price, not static 75
    assert report[0]["status"] == "assumed"
    assert "uncontested" in report[0]["detail"]


def test_reprice_simulation_skips_when_budget_below_open(monkeypatch):
    def boom(nft):
        raise AssertionError("order book must not be read in simulation")

    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    monkeypatch.setattr(engine, "_fetch_card_activity", boom)
    plan = _offer_plan(budget=60.0)  # cannot cover the open price (70)
    report = engine._reprice_offers_dynamically(plan, read_book=False)
    assert plan.offers == []
    assert report[0]["status"] == "skipped"


def test_demo_cycle_reports_offer_pricing_when_enabled():
    # A demo cycle now surfaces the dynamic offer pricing (read_book=False), so
    # the simulation reflects the configured range instead of silently using the
    # static bid. With no sourced listings the list is simply empty.
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True))
    report = engine.run_cycle(sim_volume=500.0)
    assert report["demo"] is True
    assert report["offer_pricing"] == []


def test_run_cycle_reports_offer_pricing_when_enabled(store, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(_dyn_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()  # no sourced offers -> empty reprice list
    assert report["offer_pricing"] == []


def test_run_cycle_no_offer_pricing_when_disabled(store, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert "offer_pricing" not in report  # key omitted when feature is off


# --------------------------------------------------------------------------- #
# Self-bidding guard (offer-bump fix): never bump when already highest
# --------------------------------------------------------------------------- #
def test_bump_skips_when_we_are_highest_bidder(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_card_activity"] = {
        "data": [_bid("WALLEThdrtest", 50.0)]}  # our own wallet leads
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_aged_open_offer(store, nft="O1", price=8.0)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert len(report["bumped"]) == 1
    assert report["bumped"][0]["status"] == "skipped"
    assert "already highest bidder" in report["bumped"][0]["detail"]
    assert "update_offer" not in fake.call_names()  # no bump sent
    persisted = store.open_offers()
    assert persisted[0].bump_count == 0  # offer untouched


def test_bump_proceeds_when_competitor_is_highest(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_card_activity"] = {
        "data": [_bid("WB", 50.0)]}  # someone else leads
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_aged_open_offer(store, nft="O1", price=8.0)
    engine = make_engine(armed_cfg(), wallet=FakeWallet(can_sign=True),
                        store=store)
    report = engine.run_cycle()
    assert report["bumped"][0]["bump_count"] == 1
    assert "update_offer" in fake.call_names()  # bump went out


# --------------------------------------------------------------------------- #
# Markdown gas guard + jitter wiring
# --------------------------------------------------------------------------- #
def _seed_aged_listing(store, *, nft, list_price, market_at_buy, cost=10.0):
    import time as _t
    old = _t.time() - 100 * 86400.0
    store.upsert_holding(Holding(
        nft=nft, name="Card", category="Pokemon", acquired_at=old,
        cost_usd=cost, market_usd_at_buy=market_at_buy, status="listed",
        list_price_usd=list_price, listed_at=old))


def test_markdown_skips_change_below_minimum(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "M1"}], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_aged_listing(store, nft="M1", list_price=20.0, market_at_buy=20.0)
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      max_consecutive_failures=3, markdown_step_pct=1.0,
                      markdown_min_change_usd=0.25)  # step 0.20 < 0.25
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True), store=store)
    report = engine.run_cycle()
    assert len(report["marked_down"]) == 1
    assert report["marked_down"][0]["status"] == "skipped"
    assert "below minimum" in report["marked_down"][0]["detail"]
    assert "update_listing" not in fake.call_names()  # no tx signed
    held = store.get_holding("M1")
    assert held.list_price_usd == 20.0  # unchanged
    assert held.markdown_steps == 0


def test_markdown_with_jitter_still_steps_down(store, monkeypatch):
    fake = FakeClient()
    fake.responses["get_owned_cards"] = {
        "filterNFtCard": [{"nftAddress": "M2"}], "totalPages": 1}
    monkeypatch.setattr("collectorcrypt.trader.engine.CCTradingClient",
                        lambda **kw: fake)
    _seed_aged_listing(store, nft="M2", list_price=20.0, market_at_buy=100.0)
    cfg = make_config(live=True, auth_provider="static", cc_token="tok",
                      max_consecutive_failures=3, markdown_step_pct=5.0,
                      markdown_jitter_pct=20.0)  # base step 5.0, ±20%
    engine = make_engine(cfg, wallet=FakeWallet(can_sign=True), store=store)
    report = engine.run_cycle()
    assert len(report["marked_down"]) == 1
    assert report["marked_down"][0]["status"] == "confirmed"
    assert "update_listing" in fake.call_names()
    held = store.get_holding("M2")
    assert held.list_price_usd < 20.0           # stepped down
    assert 13.0 < held.list_price_usd < 17.0    # base 15 ± 20% step jitter
    assert held.markdown_steps == 1

