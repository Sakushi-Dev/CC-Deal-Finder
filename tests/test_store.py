"""OrderStore persistence and query tests.

The store is the trader's source of truth — idempotency (UNIQUE
``client_order_id``), correct lifecycle queries, and the risk-usage aggregates
all live or die here. Each test uses an isolated temp database (``store``
fixture) so nothing touches the real workspace DB.
"""
from __future__ import annotations

import time

import pytest

from collectorcrypt.trader.orders import Order, OrderKind, OrderStatus

from .conftest import make_buy, make_list, make_offer


# --------------------------------------------------------------------------- #
# Idempotent upsert
# --------------------------------------------------------------------------- #
def test_upsert_and_read_back(store):
    o = make_buy(nft="N", price_usd=10, cycle_id="c")
    store.upsert_order(o)
    got = store.get_by_client_order_id(o.client_order_id)
    assert got is not None
    assert got.nft == "N"
    assert got.price_usd == 10


def test_upsert_same_intent_is_idempotent(store):
    o1 = make_buy(nft="N", price_usd=10, cycle_id="c")
    store.upsert_order(o1)
    o2 = make_buy(nft="N", price_usd=10, cycle_id="c")  # same client_order_id
    o2.transition(OrderStatus.CONFIRMED)
    store.upsert_order(o2)
    assert len(store.active_orders()) == 0
    got = store.get_by_client_order_id(o1.client_order_id)
    assert got.status is OrderStatus.CONFIRMED


def test_upsert_updates_status(store):
    o = make_buy(nft="N", cycle_id="c")
    store.upsert_order(o)
    o.transition(OrderStatus.SUBMITTED)
    store.upsert_order(o)
    assert store.get_by_client_order_id(o.client_order_id).status is OrderStatus.SUBMITTED


def test_get_by_unknown_id_returns_none(store):
    assert store.get_by_client_order_id("nope") is None


def test_get_order_by_id(store):
    o = make_buy(nft="N", cycle_id="c")
    store.upsert_order(o)
    assert store.get_order(o.id).id == o.id


def test_save_orders_bulk(store):
    orders = [make_buy(nft=f"N{i}", cycle_id="c") for i in range(5)]
    store.save_orders(orders)
    assert len(store.active_orders()) == 0  # all PLANNED, not active
    assert store.get_by_client_order_id(orders[0].client_order_id) is not None


def test_save_orders_empty_noop(store):
    store.save_orders([])  # must not raise


def test_roundtrip_preserves_history(store):
    o = make_buy(nft="N", cycle_id="c")
    o.transition(OrderStatus.SUBMITTED, detail="sent")
    o.transition(OrderStatus.SIGNED)
    store.upsert_order(o)
    got = store.get_by_client_order_id(o.client_order_id)
    assert [h["to"] for h in got.history] == ["submitted", "signed"]


# --------------------------------------------------------------------------- #
# Lifecycle queries
# --------------------------------------------------------------------------- #
def test_active_orders_only_in_flight(store):
    planned = make_buy(nft="P", cycle_id="c")
    submitted = make_buy(nft="S", cycle_id="c")
    submitted.transition(OrderStatus.SUBMITTED)
    confirmed = make_buy(nft="C", cycle_id="c")
    confirmed.transition(OrderStatus.CONFIRMED)
    store.save_orders([planned, submitted, confirmed])
    active = {o.nft for o in store.active_orders()}
    assert active == {"S"}


def test_open_offers_query(store):
    offer = make_offer(nft="O", cycle_id="c")
    offer.transition(OrderStatus.OPEN)
    other = make_offer(nft="O2", cycle_id="c")  # still PLANNED
    store.save_orders([offer, other])
    offers = store.open_offers()
    assert len(offers) == 1
    assert offers[0].nft == "O"


def test_relist_candidates_query(store):
    relist = make_list(nft="R", price_usd=25, cycle_id="c")  # PLANNED LIST
    confirmed_list = make_list(nft="R2", price_usd=25, cycle_id="c")
    confirmed_list.transition(OrderStatus.CONFIRMED)
    store.save_orders([relist, confirmed_list])
    candidates = store.relist_candidates()
    assert len(candidates) == 1
    assert candidates[0].nft == "R"


def test_orders_for_cycle(store):
    store.save_orders([make_buy(nft="A", cycle_id="c1"),
                       make_buy(nft="B", cycle_id="c1"),
                       make_buy(nft="C", cycle_id="c2")])
    assert len(store.orders_for_cycle("c1")) == 2


def test_counts_by_status(store):
    a = make_buy(nft="A", cycle_id="c")
    b = make_buy(nft="B", cycle_id="c")
    b.transition(OrderStatus.CONFIRMED)
    store.save_orders([a, b])
    counts = store.counts_by_status()
    assert counts.get("planned") == 1
    assert counts.get("confirmed") == 1


# --------------------------------------------------------------------------- #
# Risk-usage aggregates
# --------------------------------------------------------------------------- #
def test_confirmed_spend_since_sums_real_buys(store):
    a = make_buy(nft="A", price_usd=10, cycle_id="c", simulated=False)
    a.transition(OrderStatus.CONFIRMED)
    b = make_offer(nft="B", price_usd=5, cycle_id="c", simulated=False)
    b.transition(OrderStatus.CONFIRMED)
    store.save_orders([a, b])
    assert store.confirmed_spend_since(0) == 15.0


def test_confirmed_spend_excludes_simulated(store):
    sim = make_buy(nft="A", price_usd=10, cycle_id="c", simulated=True)
    sim.transition(OrderStatus.CONFIRMED)
    store.upsert_order(sim)
    assert store.confirmed_spend_since(0) == 0.0


def test_confirmed_spend_excludes_unconfirmed(store):
    pending = make_buy(nft="A", price_usd=10, cycle_id="c", simulated=False)
    pending.transition(OrderStatus.PENDING)
    store.upsert_order(pending)
    assert store.confirmed_spend_since(0) == 0.0


def test_confirmed_spend_respects_time_window(store):
    old = make_buy(nft="A", price_usd=10, cycle_id="c", simulated=False)
    old.transition(OrderStatus.CONFIRMED)
    old.created_at = time.time() - 100000  # outside window
    store.upsert_order(old)
    assert store.confirmed_spend_since(time.time() - 3600) == 0.0


def test_open_position_count_real_only(store):
    real = make_buy(nft="A", cycle_id="c", simulated=False)
    real.transition(OrderStatus.SUBMITTED)
    sim = make_buy(nft="B", cycle_id="c", simulated=True)
    sim.transition(OrderStatus.SUBMITTED)
    store.save_orders([real, sim])
    assert store.open_position_count() == 1


def test_recent_terminal_statuses_real_only_newest_first(store):
    a = make_buy(nft="A", cycle_id="c", simulated=False)
    a.transition(OrderStatus.FAILED)
    store.upsert_order(a)
    time.sleep(0.01)
    b = make_buy(nft="B", cycle_id="c", simulated=False)
    b.transition(OrderStatus.CONFIRMED)
    store.upsert_order(b)
    statuses = store.recent_terminal_statuses()
    assert statuses[0] == "confirmed"  # newest first
    assert "failed" in statuses


def test_recent_terminal_excludes_active(store):
    active = make_buy(nft="A", cycle_id="c", simulated=False)
    active.transition(OrderStatus.PENDING)
    store.upsert_order(active)
    assert store.recent_terminal_statuses() == []


# --------------------------------------------------------------------------- #
# Cycles
# --------------------------------------------------------------------------- #
def test_save_and_read_cycle(store):
    store.save_cycle("cyc1", mode="live", wallet="W", demo=False,
                     config_snapshot={"live": True}, summary={"buys": 2})
    cycles = store.recent_cycles()
    assert len(cycles) == 1
    assert cycles[0]["cycle_id"] == "cyc1"
    assert cycles[0]["buys"] == 2


def test_save_cycle_idempotent(store):
    store.save_cycle("cyc1", mode="live", wallet="W", demo=False,
                     config_snapshot={}, summary={"v": 1})
    store.save_cycle("cyc1", mode="live", wallet="W", demo=False,
                     config_snapshot={}, summary={"v": 2})
    cycles = store.recent_cycles()
    assert len(cycles) == 1
    assert cycles[0]["v"] == 2


def test_recent_cycles_oldest_first(store):
    store.save_cycle("c1", mode="m", wallet="W", demo=False,
                     config_snapshot={}, summary={})
    time.sleep(0.01)
    store.save_cycle("c2", mode="m", wallet="W", demo=False,
                     config_snapshot={}, summary={})
    cycles = store.recent_cycles()
    assert [c["cycle_id"] for c in cycles] == ["c1", "c2"]


# --------------------------------------------------------------------------- #
# Runtime KV
# --------------------------------------------------------------------------- #
def test_runtime_set_get(store):
    store.set_runtime("loop_state", {"active": True, "interval": 60})
    assert store.get_runtime("loop_state") == {"active": True, "interval": 60}


def test_runtime_default_when_absent(store):
    assert store.get_runtime("missing", "fallback") == "fallback"


def test_runtime_upsert_overwrites(store):
    store.set_runtime("k", {"v": 1})
    store.set_runtime("k", {"v": 2})
    assert store.get_runtime("k") == {"v": 2}


def test_runtime_stores_various_types(store):
    store.set_runtime("a", [1, 2, 3])
    store.set_runtime("b", "string")
    store.set_runtime("c", 42)
    assert store.get_runtime("a") == [1, 2, 3]
    assert store.get_runtime("b") == "string"
    assert store.get_runtime("c") == 42


# --------------------------------------------------------------------------- #
# Persistence across instances (durability)
# --------------------------------------------------------------------------- #
def test_data_survives_new_store_instance(tmp_path):
    from collectorcrypt.trader.store import OrderStore

    db = str(tmp_path / "dur.db")
    s1 = OrderStore(db)
    o = make_buy(nft="N", price_usd=10, cycle_id="c")
    s1.upsert_order(o)
    # A fresh instance on the same file sees the data.
    s2 = OrderStore(db)
    assert s2.get_by_client_order_id(o.client_order_id) is not None
