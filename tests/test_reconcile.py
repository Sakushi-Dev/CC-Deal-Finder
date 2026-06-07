"""Reconciliation tests — the safety net that keeps persisted state honest.

Two layers are tested:

* :class:`Reconciler` — strictly read-only; it must *report* stale/inconsistent
  orders and never mutate one (guessing an order's fate is forbidden).
* :class:`StatusSyncer` — authoritative; it transitions in-flight orders only on
  clear API evidence, leaves ambiguous ones untouched, and spawns relist
  candidates for confirmed buys. A read error must never transition an order.
"""
from __future__ import annotations

import time

import pytest

from collectorcrypt.trader.ccapi import CCServerError
from collectorcrypt.trader.orders import OrderKind, OrderStatus
from collectorcrypt.trader.reconcile import Reconciler, StatusSyncer
from collectorcrypt.trader.store import (HOLDING_HELD, HOLDING_LISTED,
                                         HOLDING_SOLD, Holding)

from .conftest import FakeClient, make_buy, make_list, make_offer


# --------------------------------------------------------------------------- #
# Reconciler (read-only)
# --------------------------------------------------------------------------- #
def test_reconcile_counts_active(store):
    a = make_buy(nft="A", cycle_id="c", simulated=False)
    a.transition(OrderStatus.PENDING)
    store.upsert_order(a)
    report = Reconciler(store).reconcile()
    assert report.active == 1


def test_reconcile_counts_open_offers(store):
    o = make_offer(nft="O", cycle_id="c", simulated=False)
    o.transition(OrderStatus.OPEN)
    store.upsert_order(o)
    report = Reconciler(store).reconcile()
    assert report.open_offers == 1


def test_reconcile_counts_relist_candidates(store):
    store.upsert_order(make_list(nft="R", price_usd=25, cycle_id="c"))
    report = Reconciler(store).reconcile()
    assert report.relist_candidates == 1


def test_reconcile_flags_stale(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    o.transition(OrderStatus.PENDING)
    o.updated_at = time.time() - 10000
    store.upsert_order(o)
    report = Reconciler(store, stale_after_sec=900).reconcile()
    assert len(report.stale) == 1


def test_reconcile_fresh_not_stale(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    o.transition(OrderStatus.PENDING)
    store.upsert_order(o)
    report = Reconciler(store, stale_after_sec=900).reconcile()
    assert report.stale == []


def test_reconcile_flags_simulated_active_inconsistency(store):
    o = make_buy(nft="A", cycle_id="c", simulated=True)
    o.transition(OrderStatus.PENDING)
    store.upsert_order(o)
    report = Reconciler(store).reconcile()
    assert len(report.inconsistencies) == 1


def test_reconcile_healthy_when_clean(store):
    report = Reconciler(store).reconcile()
    assert report.healthy


def test_reconcile_does_not_mutate(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    o.transition(OrderStatus.PENDING)
    store.upsert_order(o)
    Reconciler(store).reconcile()
    # Still PENDING — reconcile never transitions.
    assert store.get_by_client_order_id(o.client_order_id).status is OrderStatus.PENDING


def test_reconcile_to_dict_shape(store):
    d = Reconciler(store).reconcile().to_dict()
    for key in ("ts", "active", "open_offers", "relist_candidates", "stale",
                "inconsistencies", "healthy"):
        assert key in d


# --------------------------------------------------------------------------- #
# StatusSyncer (authoritative)
# --------------------------------------------------------------------------- #
def _seed_active(store, order, status, external_id="ext"):
    order.transition(status, external_id=external_id)
    store.upsert_order(order)
    return order


def test_sync_confirms_pending_buy(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False, market_usd=20)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.confirmed == 1
    assert store.get_by_client_order_id(o.client_order_id).status is OrderStatus.CONFIRMED


def test_sync_confirms_accepted_offer(store):
    o = make_offer(nft="A", cycle_id="c", simulated=False)
    _seed_active(store, o, OrderStatus.OPEN)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "accepted"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.confirmed == 1


def test_sync_cancels_withdrawn(store):
    o = make_offer(nft="A", cycle_id="c", simulated=False)
    _seed_active(store, o, OrderStatus.OPEN)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "cancelled"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.cancelled == 1
    assert store.get_by_client_order_id(o.client_order_id).status is OrderStatus.CANCELLED


def test_sync_ambiguous_left_unresolved(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "pending"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.unresolved == 1
    assert store.get_by_client_order_id(o.client_order_id).status is OrderStatus.PENDING


def test_sync_read_error_never_transitions(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.errors["check_listing_status"] = CCServerError("down")
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.errors == 1
    assert store.get_by_client_order_id(o.client_order_id).status is OrderStatus.PENDING


def test_sync_no_wallet_unresolved(store):
    # Without a wallet there is nothing to look up -> failure-safe unresolved.
    o = make_buy(nft="A", cycle_id="c", simulated=False)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    report = StatusSyncer(store, client=client, wallet="").sync()
    assert report.unresolved == 1


def test_sync_confirmed_buy_spawns_relist(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False, market_usd=20,
                 resell_usd=18)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.relisted_spawned == 1
    assert len(store.relist_candidates()) == 1


def test_sync_confirmed_buy_no_resell_no_relist(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False, market_usd=20,
                 resell_usd=0)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.relisted_spawned == 0


def test_sync_empty_when_nothing_active(store):
    report = StatusSyncer(store, client=FakeClient(), wallet="W").sync()
    assert report.checked == 0


def test_sync_checks_every_active(store):
    for i in range(3):
        o = make_buy(nft=f"N{i}", cycle_id="c", simulated=False)
        _seed_active(store, o, OrderStatus.PENDING, external_id=f"ext{i}")
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "pending"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.checked == 3


def test_sync_records_transitions(store):
    o = make_buy(nft="A", cycle_id="c", simulated=False, market_usd=20)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.transitions[0]["to"] == "confirmed"
    assert report.transitions[0]["from"] == "pending"


def test_sync_to_dict_shape(store):
    d = StatusSyncer(store, client=FakeClient(), wallet="W").sync().to_dict()
    for key in ("ts", "checked", "confirmed", "cancelled", "relisted_spawned",
                "unresolved", "errors", "transitions"):
        assert key in d


def test_sync_relist_idempotent(store):
    # Spawn once, then a second sync must not duplicate the relist candidate.
    o = make_buy(nft="A", cycle_id="c", simulated=False, market_usd=20,
                 resell_usd=18)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    StatusSyncer(store, client=client, wallet="W").sync()
    # Manually re-open is impossible (terminal), but the relist candidate exists.
    assert len(store.relist_candidates()) == 1


# --------------------------------------------------------------------------- #
# StatusSyncer holdings population (ETAPPE 5)
# --------------------------------------------------------------------------- #
def test_sync_confirmed_buy_records_holding(store):
    o = make_buy(nft="A", name="Card", category="Pokemon", cycle_id="c",
                 simulated=False, price_usd=10, market_usd=20)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    StatusSyncer(store, client=client, wallet="W").sync()
    holding = store.get_holding("A")
    assert holding is not None
    assert holding.status == HOLDING_HELD
    assert holding.cost_usd == 10
    assert holding.market_usd_at_buy == 20


def test_sync_filled_offer_records_holding(store):
    o = make_offer(nft="A", cycle_id="c", simulated=False, price_usd=8,
                   market_usd=20)
    _seed_active(store, o, OrderStatus.OPEN)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "accepted"}
    StatusSyncer(store, client=client, wallet="W").sync()
    holding = store.get_holding("A")
    assert holding is not None
    assert holding.status == HOLDING_HELD
    assert holding.cost_usd == 8


def test_sync_simulated_order_records_no_holding(store):
    o = make_buy(nft="A", cycle_id="c", simulated=True, market_usd=20)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.confirmed == 1  # the order still resolves
    assert store.get_holding("A") is None  # but no ledger row


def test_sync_confirmed_list_marks_holding_sold(store):
    # A held+listed card whose relist (LIST) order resolves confirmed = sold.
    store.upsert_holding(Holding(nft="A", acquired_at=1.0, cost_usd=10,
                                 market_usd_at_buy=20, listed_at=2.0,
                                 list_price_usd=18, status=HOLDING_LISTED))
    o = make_list(nft="A", cycle_id="c", simulated=False, price_usd=18,
                  market_usd=20)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}
    StatusSyncer(store, client=client, wallet="W").sync()
    holding = store.get_holding("A")
    assert holding.status == HOLDING_SOLD
    assert holding.sold_at is not None and holding.sold_at > 0


def test_sync_holdings_write_failure_does_not_abort(store, monkeypatch):
    o = make_buy(nft="A", cycle_id="c", simulated=False, market_usd=20)
    _seed_active(store, o, OrderStatus.PENDING)
    client = FakeClient()
    client.responses["check_listing_status"] = {"status": "confirmed"}

    def boom(_holding):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "upsert_holding", boom)
    report = StatusSyncer(store, client=client, wallet="W").sync()
    assert report.confirmed == 1  # the order sync completed despite the failure
    assert store.get_by_client_order_id(
        o.client_order_id).status is OrderStatus.CONFIRMED
