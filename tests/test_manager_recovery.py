"""TraderManager crash-recovery tests (ETAPPE 8).

The safety property under test: a process restart must NEVER silently arm or
resume trading. The loop is only resumed when BOTH the operator opted in
(``TRADER_AUTO_RESUME=true``, env-only) AND the loop was actually active before
the restart. The startup reconcile is strictly read-only.

The worker loop is stubbed to a no-op so no test ever runs a real cycle or
touches the network; we assert the *control state* the recovery sets.
"""
from __future__ import annotations

import pytest

from collectorcrypt.trader import manager as mgrmod
from collectorcrypt.trader import store as storemod
from collectorcrypt.trader.manager import _LOOP_STATE_KEY, TraderManager
from collectorcrypt.trader.orders import OrderStatus
from collectorcrypt.trader.store import OrderStore

from .conftest import make_buy


@pytest.fixture
def mgr_env(tmp_path, monkeypatch):
    """Isolate the store + settings on temp paths and stub the worker loop.

    Returns the shared store path so a test can pre-seed loop state / orders
    before constructing the manager.
    """
    db_path = tmp_path / "trader_store.db"
    settings_path = tmp_path / "trader_settings.json"
    # OrderStore() with no arg uses the module-level STORE_PATH constant.
    monkeypatch.setattr(storemod, "STORE_PATH", db_path)
    monkeypatch.setenv("TRADER_STORE_PATH", str(db_path))
    monkeypatch.setenv("TRADER_SETTINGS_PATH", str(settings_path))
    # Never run a real cycle/loop during recovery tests.
    monkeypatch.setattr(TraderManager, "_loop", lambda self: None)
    # Clean the live/auth/resume env so each test is deterministic.
    for key in ("TRADER_AUTO_RESUME", "TRADER_LIVE", "TRADER_AUTH_PROVIDER",
                "TRADER_WALLET_SECRET"):
        monkeypatch.delenv(key, raising=False)
    return db_path, monkeypatch


def _seed_loop_state(db_path, **state):
    store = OrderStore(str(db_path))
    store.set_runtime(_LOOP_STATE_KEY, state)


# --------------------------------------------------------------------------- #
# No prior state
# --------------------------------------------------------------------------- #
def test_fresh_start_no_resume(mgr_env):
    _db, _ = mgr_env
    mgr = TraderManager()
    assert mgr._recovery["performed"] is True
    assert mgr._recovery["was_active"] is False
    assert mgr._recovery["resumed"] is False
    assert mgr._loop_active is False


def test_recovery_summary_keys(mgr_env):
    _db, _ = mgr_env
    mgr = TraderManager()
    for key in ("performed", "auto_resume", "resumed", "in_flight", "was_active"):
        assert key in mgr._recovery


# --------------------------------------------------------------------------- #
# Auto-resume gating (the core safety property)
# --------------------------------------------------------------------------- #
def test_no_resume_when_autoresume_off(mgr_env):
    db_path, _ = mgr_env
    _seed_loop_state(db_path, loop_active=True, paused=False, interval=60.0)
    # TRADER_AUTO_RESUME is unset (off) -> must NOT resume despite prior activity.
    mgr = TraderManager()
    assert mgr._recovery["was_active"] is True
    assert mgr._recovery["resumed"] is False
    assert mgr._loop_active is False


def test_resume_when_autoresume_on_and_was_active(mgr_env):
    db_path, monkeypatch = mgr_env
    _seed_loop_state(db_path, loop_active=True, paused=False, interval=60.0)
    monkeypatch.setenv("TRADER_AUTO_RESUME", "true")
    mgr = TraderManager()
    assert mgr._recovery["resumed"] is True
    assert mgr._loop_active is True


def test_no_resume_when_autoresume_on_but_was_inactive(mgr_env):
    db_path, monkeypatch = mgr_env
    _seed_loop_state(db_path, loop_active=False, paused=False, interval=60.0)
    monkeypatch.setenv("TRADER_AUTO_RESUME", "true")
    mgr = TraderManager()
    assert mgr._recovery["resumed"] is False
    assert mgr._loop_active is False


def test_autoresume_requires_literal_true(mgr_env):
    db_path, monkeypatch = mgr_env
    _seed_loop_state(db_path, loop_active=True, paused=False, interval=60.0)
    monkeypatch.setenv("TRADER_AUTO_RESUME", "1")  # not literal "true"
    mgr = TraderManager()
    assert mgr._recovery["resumed"] is False


def test_resume_restores_interval(mgr_env):
    db_path, monkeypatch = mgr_env
    _seed_loop_state(db_path, loop_active=True, paused=False, interval=120.0)
    monkeypatch.setenv("TRADER_AUTO_RESUME", "true")
    mgr = TraderManager()
    assert mgr._interval == 120.0


def test_resume_restores_paused_state(mgr_env):
    db_path, monkeypatch = mgr_env
    _seed_loop_state(db_path, loop_active=True, paused=True, interval=60.0)
    monkeypatch.setenv("TRADER_AUTO_RESUME", "true")
    mgr = TraderManager()
    assert mgr._paused is True


def test_resume_clamps_interval_minimum(mgr_env):
    db_path, monkeypatch = mgr_env
    _seed_loop_state(db_path, loop_active=True, paused=False, interval=1.0)
    monkeypatch.setenv("TRADER_AUTO_RESUME", "true")
    mgr = TraderManager()
    assert mgr._interval >= 15.0


def test_autoresume_flag_reported(mgr_env):
    db_path, monkeypatch = mgr_env
    monkeypatch.setenv("TRADER_AUTO_RESUME", "true")
    mgr = TraderManager()
    assert mgr._recovery["auto_resume"] is True


# --------------------------------------------------------------------------- #
# Startup reconcile (read-only)
# --------------------------------------------------------------------------- #
def test_recovery_reports_in_flight(mgr_env):
    db_path, _ = mgr_env
    store = OrderStore(str(db_path))
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    o.transition(OrderStatus.PENDING)
    store.upsert_order(o)
    mgr = TraderManager()
    assert mgr._recovery["in_flight"] == 1


def test_recovery_reconcile_is_readonly(mgr_env):
    db_path, _ = mgr_env
    store = OrderStore(str(db_path))
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    o.transition(OrderStatus.PENDING)
    store.upsert_order(o)
    TraderManager()  # constructing runs the startup reconcile
    # The order must still be PENDING — reconcile never transitions.
    assert store.get_by_client_order_id(o.client_order_id).status is OrderStatus.PENDING


# --------------------------------------------------------------------------- #
# Loop-state persistence via controls
# --------------------------------------------------------------------------- #
def test_start_loop_persists_state(mgr_env):
    db_path, _ = mgr_env
    mgr = TraderManager()
    mgr.start_loop(45.0)
    saved = OrderStore(str(db_path)).get_runtime(_LOOP_STATE_KEY)
    assert saved["loop_active"] is True
    assert saved["interval"] == 45.0


def test_stop_persists_inactive(mgr_env):
    db_path, _ = mgr_env
    mgr = TraderManager()
    mgr.start_loop(45.0)
    mgr.stop()
    saved = OrderStore(str(db_path)).get_runtime(_LOOP_STATE_KEY)
    assert saved["loop_active"] is False


def test_pause_persists_paused(mgr_env):
    db_path, _ = mgr_env
    mgr = TraderManager()
    mgr.start_loop(45.0)
    mgr.pause()
    saved = OrderStore(str(db_path)).get_runtime(_LOOP_STATE_KEY)
    assert saved["paused"] is True


def test_resume_control_persists_unpaused(mgr_env):
    db_path, _ = mgr_env
    mgr = TraderManager()
    mgr.start_loop(45.0)
    mgr.pause()
    mgr.resume()
    saved = OrderStore(str(db_path)).get_runtime(_LOOP_STATE_KEY)
    assert saved["paused"] is False


def test_persisted_state_survives_restart(mgr_env):
    db_path, monkeypatch = mgr_env
    mgr = TraderManager()
    mgr.start_loop(90.0)
    # Simulate a restart with auto-resume on.
    monkeypatch.setenv("TRADER_AUTO_RESUME", "true")
    mgr2 = TraderManager()
    assert mgr2._recovery["resumed"] is True
    assert mgr2._interval == 90.0
