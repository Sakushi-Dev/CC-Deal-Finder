"""Order domain-model and lifecycle state-machine tests.

The order lifecycle is the spine of the live pipeline: the live executor walks
real orders ``SUBMITTED -> SIGNED -> PENDING -> CONFIRMED`` and a single illegal
or "reviving" transition could double-spend or resurrect a closed position.
These tests pin every legal and illegal edge of the state machine.
"""
from __future__ import annotations

import itertools

import pytest

from collectorcrypt.trader.orders import (ACTIVE_STATUSES, TERMINAL_STATUSES,
                                          Order, OrderError, OrderKind,
                                          OrderStatus, make_client_order_id,
                                          plan_to_orders, relist_order_for)
from collectorcrypt.trader.strategy import BuyPlan, Candidate, Offer

ALL_STATUSES = list(OrderStatus)

# The authoritative allowed-transition map mirrored from the implementation.
LEGAL = {
    OrderStatus.PLANNED: {OrderStatus.SUBMITTED, OrderStatus.SIGNED,
                          OrderStatus.PENDING, OrderStatus.OPEN,
                          OrderStatus.CONFIRMED, OrderStatus.FAILED,
                          OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {OrderStatus.SIGNED, OrderStatus.PENDING,
                            OrderStatus.OPEN, OrderStatus.CONFIRMED,
                            OrderStatus.FAILED, OrderStatus.CANCELLED},
    OrderStatus.SIGNED: {OrderStatus.PENDING, OrderStatus.CONFIRMED,
                         OrderStatus.FAILED, OrderStatus.CANCELLED},
    OrderStatus.PENDING: {OrderStatus.OPEN, OrderStatus.CONFIRMED,
                          OrderStatus.FAILED, OrderStatus.CANCELLED},
    OrderStatus.OPEN: {OrderStatus.CONFIRMED, OrderStatus.FAILED,
                       OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: set(),
    OrderStatus.FAILED: set(),
    OrderStatus.CANCELLED: set(),
}

LEGAL_PAIRS = [(a, b) for a in ALL_STATUSES for b in LEGAL[a]]
ILLEGAL_PAIRS = [(a, b) for a in ALL_STATUSES for b in ALL_STATUSES
                 if b not in LEGAL[a]]


def _order_in(status: OrderStatus, kind: OrderKind = OrderKind.BUY) -> Order:
    """Build an order forced into ``status`` via a legal path from PLANNED."""
    order = Order(kind=kind, nft="NFT", cycle_id="c")
    if status is OrderStatus.PLANNED:
        return order
    # PLANNED can shortcut to any state directly.
    order.transition(status)
    return order


# --------------------------------------------------------------------------- #
# Transition matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("src,dst", LEGAL_PAIRS)
def test_legal_transition_is_allowed(src, dst):
    order = _order_in(src)
    order.transition(dst)
    assert order.status is dst


@pytest.mark.parametrize("src,dst", ILLEGAL_PAIRS)
def test_illegal_transition_raises(src, dst):
    order = _order_in(src)
    with pytest.raises(OrderError):
        order.transition(dst)
    # The order must not have moved.
    assert order.status is src


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value))
@pytest.mark.parametrize("target", ALL_STATUSES)
def test_terminal_states_are_final(terminal, target):
    order = _order_in(terminal)
    with pytest.raises(OrderError):
        order.transition(target)
    assert order.status is terminal


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_active_flag_matches_set(status):
    order = _order_in(status)
    assert order.is_active is (status in ACTIVE_STATUSES)


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_terminal_flag_matches_set(status):
    order = _order_in(status)
    assert order.is_terminal is (status in TERMINAL_STATUSES)


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_succeeded_flag(status):
    order = _order_in(status)
    assert order.succeeded is (status in (OrderStatus.CONFIRMED, OrderStatus.OPEN))


# --------------------------------------------------------------------------- #
# Transition side effects
# --------------------------------------------------------------------------- #
def test_transition_records_history():
    order = Order(kind=OrderKind.BUY, nft="N", cycle_id="c")
    order.transition(OrderStatus.SUBMITTED, detail="sent")
    order.transition(OrderStatus.SIGNED)
    order.transition(OrderStatus.PENDING)
    order.transition(OrderStatus.CONFIRMED, detail="done")
    assert [h["to"] for h in order.history] == [
        "submitted", "signed", "pending", "confirmed"]
    assert order.history[0]["from"] == "planned"
    assert order.history[0]["detail"] == "sent"


def test_transition_updates_fields_atomically():
    order = Order(kind=OrderKind.BUY, nft="N", cycle_id="c")
    order.transition(OrderStatus.SUBMITTED, external_id="rcpt",
                     signature="sig", detail="d", error="")
    assert order.external_id == "rcpt"
    assert order.signature == "sig"
    assert order.detail == "d"


def test_transition_returns_self():
    order = Order(kind=OrderKind.BUY, nft="N", cycle_id="c")
    assert order.transition(OrderStatus.CONFIRMED) is order


def test_failed_transition_does_not_append_history():
    order = _order_in(OrderStatus.CONFIRMED)
    before = len(order.history)
    with pytest.raises(OrderError):
        order.transition(OrderStatus.FAILED)
    assert len(order.history) == before


# --------------------------------------------------------------------------- #
# client_order_id idempotency key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", list(OrderKind))
def test_client_order_id_autoderived(kind):
    order = Order(kind=kind, nft="ABC", cycle_id="cyc")
    assert order.client_order_id == f"cyc:{kind.value}:ABC"


@pytest.mark.parametrize("kind", list(OrderKind))
def test_make_client_order_id_matches(kind):
    assert make_client_order_id("c", kind, "n") == f"c:{kind.value}:n"


def test_client_order_id_not_derived_without_cycle():
    order = Order(kind=OrderKind.BUY, nft="ABC")
    assert order.client_order_id == ""


def test_client_order_id_explicit_preserved():
    order = Order(kind=OrderKind.BUY, nft="ABC", cycle_id="cyc",
                  client_order_id="custom")
    assert order.client_order_id == "custom"


def test_same_intent_same_key():
    a = Order(kind=OrderKind.BUY, nft="X", cycle_id="c1")
    b = Order(kind=OrderKind.BUY, nft="X", cycle_id="c1")
    assert a.client_order_id == b.client_order_id


def test_different_cycle_different_key():
    a = Order(kind=OrderKind.BUY, nft="X", cycle_id="c1")
    b = Order(kind=OrderKind.BUY, nft="X", cycle_id="c2")
    assert a.client_order_id != b.client_order_id


# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #
def test_is_open_offer_true():
    o = _order_in(OrderStatus.OPEN, kind=OrderKind.OFFER)
    assert o.is_open_offer


@pytest.mark.parametrize("kind", [OrderKind.BUY, OrderKind.LIST])
def test_is_open_offer_false_other_kinds(kind):
    o = _order_in(OrderStatus.OPEN, kind=kind)
    assert not o.is_open_offer


def test_is_relist_candidate_true():
    o = Order(kind=OrderKind.LIST, nft="N", cycle_id="c")
    assert o.is_relist_candidate


@pytest.mark.parametrize("status", [OrderStatus.SUBMITTED, OrderStatus.CONFIRMED])
def test_is_relist_candidate_false_when_not_planned(status):
    o = _order_in(status, kind=OrderKind.LIST)
    assert not o.is_relist_candidate


# --------------------------------------------------------------------------- #
# Serialization round-trip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", list(OrderKind))
@pytest.mark.parametrize("status", ALL_STATUSES)
def test_to_from_dict_roundtrip(kind, status):
    order = _order_in(status, kind=kind)
    order.price_usd = 12.5
    order.market_usd = 30.0
    order.resell_usd = 25.0
    order.external_id = "ext"
    restored = Order.from_dict(order.to_dict())
    assert restored.kind is kind
    assert restored.status is status
    assert restored.nft == order.nft
    assert restored.price_usd == 12.5
    assert restored.market_usd == 30.0
    assert restored.resell_usd == 25.0
    assert restored.external_id == "ext"
    assert restored.client_order_id == order.client_order_id


def test_to_dict_contains_all_keys():
    order = Order(kind=OrderKind.BUY, nft="N", cycle_id="c")
    d = order.to_dict()
    for key in ("id", "cycle_id", "client_order_id", "parent_id", "kind",
                "status", "nft", "price_usd", "market_usd", "resell_usd",
                "simulated", "external_id", "signature", "history"):
        assert key in d


def test_from_dict_defaults_missing_fields():
    order = Order.from_dict({"kind": "buy"})
    assert order.kind is OrderKind.BUY
    assert order.status is OrderStatus.PLANNED
    assert order.price_usd == 0.0
    assert order.id  # a fresh id was generated


# --------------------------------------------------------------------------- #
# plan_to_orders / relist_order_for
# --------------------------------------------------------------------------- #
def _candidate(nft="N", ask=10.0, market=20.0, resell=18.0):
    return Candidate(card={"nft": nft, "name": nft, "category": "Pokemon"},
                     ask_usd=ask, market_usd=market, discount_pct=50.0,
                     resell_usd=resell)


def test_plan_to_orders_builds_buys_and_offers():
    c1 = _candidate("A")
    c2 = _candidate("B")
    plan = BuyPlan(items=[c1], offers=[Offer(c2, 8.0)])
    orders = plan_to_orders(plan, "cyc", simulated=False)
    kinds = sorted(o.kind.value for o in orders)
    assert kinds == ["buy", "offer"]


def test_plan_to_orders_never_creates_list_orders():
    plan = BuyPlan(items=[_candidate("A")], offers=[Offer(_candidate("B"), 5.0)])
    orders = plan_to_orders(plan, "cyc", simulated=False)
    assert all(o.kind is not OrderKind.LIST for o in orders)


@pytest.mark.parametrize("simulated", [True, False])
def test_plan_to_orders_simulated_flag(simulated):
    plan = BuyPlan(items=[_candidate("A")])
    orders = plan_to_orders(plan, "cyc", simulated=simulated)
    assert orders[0].simulated is simulated


def test_plan_to_orders_snapshots_economics():
    plan = BuyPlan(items=[_candidate("A", ask=10.0, market=30.0, resell=27.0)])
    order = plan_to_orders(plan, "cyc", simulated=False)[0]
    assert order.price_usd == 10.0
    assert order.market_usd == 30.0
    assert order.resell_usd == 27.0


def test_plan_to_orders_empty_plan():
    assert plan_to_orders(BuyPlan(), "cyc", simulated=False) == []


def test_relist_order_for_links_parent():
    buy = Order(kind=OrderKind.BUY, nft="N", cycle_id="c", resell_usd=25.0)
    buy.transition(OrderStatus.CONFIRMED)
    relist = relist_order_for(buy)
    assert relist.kind is OrderKind.LIST
    assert relist.price_usd == 25.0
    assert relist.parent_id == buy.id


def test_relist_order_for_is_planned():
    buy = Order(kind=OrderKind.BUY, nft="N", cycle_id="c", resell_usd=25.0)
    relist = relist_order_for(buy)
    assert relist.status is OrderStatus.PLANNED
    assert relist.is_relist_candidate
