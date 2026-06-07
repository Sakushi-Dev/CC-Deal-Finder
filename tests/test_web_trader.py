"""Trader dashboard surface tests (ETAPPE 7).

Covers the new UI seam: the manager snapshot exposes the holdings inventory and
the unpopular blacklist, the Flask page renders the new panels, the status JSON
carries the new keys, and the blacklist-clear route validates its input and
clears exactly one NFT. No real cycle/loop ever runs (the worker is stubbed) and
everything is isolated on a temp DB.
"""
from __future__ import annotations

import pytest

from collectorcrypt.trader import manager as mgrmod
from collectorcrypt.trader import store as storemod
from collectorcrypt.trader.manager import TraderManager
from collectorcrypt.trader.store import HOLDING_HELD, Holding, OrderStore

from collectorcrypt.web import create_app


@pytest.fixture
def trader_env(tmp_path, monkeypatch):
    """Isolate the store + settings and stub the worker loop."""
    db_path = tmp_path / "trader_store.db"
    settings_path = tmp_path / "trader_settings.json"
    monkeypatch.setattr(storemod, "STORE_PATH", db_path)
    monkeypatch.setenv("TRADER_STORE_PATH", str(db_path))
    monkeypatch.setenv("TRADER_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(TraderManager, "_loop", lambda self: None)
    for key in ("TRADER_AUTO_RESUME", "TRADER_LIVE", "TRADER_AUTH_PROVIDER",
                "TRADER_WALLET_SECRET"):
        monkeypatch.delenv(key, raising=False)
    return db_path


def _seed_holding(db_path, *, nft="HOLDNFT0000000000001", name="Charizard",
                  blacklisted=False):
    store = OrderStore(str(db_path))
    store.upsert_holding(Holding(
        nft=nft, name=name, category="Pokemon", acquired_at=1000.0,
        cost_usd=10.0, market_usd_at_buy=20.0, status=HOLDING_HELD))
    if blacklisted:
        store.mark_blacklisted(nft, now=2000.0)
    return nft


# --------------------------------------------------------------------------- #
# manager.snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_exposes_holdings_and_blacklist(trader_env):
    _seed_holding(trader_env, nft="HOLDNFT0000000000001")
    mgr = TraderManager()
    snap = mgr.snapshot()
    assert "holdings" in snap
    assert "blacklist" in snap
    assert any(h["nft"] == "HOLDNFT0000000000001" for h in snap["holdings"])


def test_snapshot_blacklist_lists_flagged(trader_env):
    _seed_holding(trader_env, nft="BLACKNFT000000000001", blacklisted=True)
    mgr = TraderManager()
    snap = mgr.snapshot()
    assert "BLACKNFT000000000001" in snap["blacklist"]


def test_snapshot_holdings_empty_by_default(trader_env):
    mgr = TraderManager()
    snap = mgr.snapshot()
    assert snap["holdings"] == []
    assert snap["blacklist"] == []


def test_clear_blacklist_entry_delegates(trader_env):
    nft = _seed_holding(trader_env, nft="BLACKNFT000000000002", blacklisted=True)
    mgr = TraderManager()
    assert nft in mgr.snapshot()["blacklist"]
    mgr.clear_blacklist_entry(nft)
    assert nft not in mgr.snapshot()["blacklist"]


# --------------------------------------------------------------------------- #
# Flask page + status JSON
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(trader_env):
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.mark.parametrize("panel_id", [
    "holdingsTable", "blacklistTable", "bumpTable", "maintBar",
    'data-tab="holdings"',
])
def test_trader_page_has_holdings_panels(client, panel_id):
    html = client.get("/trader").get_data(as_text=True)
    assert panel_id in html


def test_status_json_exposes_holdings_blacklist(client, trader_env):
    _seed_holding(trader_env, nft="HOLDNFT0000000000003")
    res = client.get("/trader/status")
    body = res.get_json()
    assert "holdings" in body
    assert "blacklist" in body


# --------------------------------------------------------------------------- #
# POST /trader/blacklist/clear
# --------------------------------------------------------------------------- #
def test_blacklist_clear_removes_entry(client, trader_env):
    nft = _seed_holding(trader_env, nft="BLACKNFT000000000004", blacklisted=True)
    res = client.post("/trader/blacklist/clear", data={"nft": nft})
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert nft not in body["state"]["blacklist"]


def test_blacklist_clear_rejects_missing_nft(client):
    res = client.post("/trader/blacklist/clear", data={})
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_blacklist_clear_rejects_invalid_nft(client):
    res = client.post("/trader/blacklist/clear", data={"nft": "../bad nft!"})
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_blacklist_clear_unknown_nft_is_ok(client):
    # A syntactically valid but unknown NFT is a harmless no-op (idempotent).
    res = client.post("/trader/blacklist/clear",
                      data={"nft": "UNKNOWNNFT00000000001"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
