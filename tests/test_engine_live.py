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

from .conftest import (FakeSessionProvider, FakeWallet, make_buy, make_config,
                       make_list, new_keypair, keypair_secret)


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
