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
                                          _order_states)
from collectorcrypt.trader.executor import DryRunExecutor, LiveExecutor
from collectorcrypt.trader.orders import OrderKind, OrderStatus
from collectorcrypt.trader.store import Holding

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


def armed_cfg():
    return make_config(live=True, auth_provider="static", cc_token="tok")


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
               "market_recheck")


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
                      offer_accept_min_market_pct=80.0)  # 80% of 20 = 16
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
