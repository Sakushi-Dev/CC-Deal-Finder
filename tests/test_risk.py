"""Risk-engine tests — the last gate before any live order is sent.

The risk engine must be fail-safe: any uncertainty resolves to *do not trade*.
These tests pin the kill switch, every spend/position cap, the LIST exemption,
and the unreadable-state halt.
"""
from __future__ import annotations

import pytest

from collectorcrypt.trader.orders import OrderKind, OrderStatus
from collectorcrypt.trader.risk import RiskEngine, _leading_failures

from .conftest import make_buy, make_config, make_list, make_offer


class FakeRiskStore:
    """In-memory risk-usage source with controllable readings."""

    def __init__(self, *, open_positions=0, spend_today=0.0, owned_cards=0,
                 statuses=None, raise_on=None):
        self._open = open_positions
        self._spend = spend_today
        self._owned = owned_cards
        self._statuses = statuses or []
        self._raise_on = raise_on or set()

    def open_position_count(self):
        if "open" in self._raise_on:
            raise RuntimeError("db down")
        return self._open

    def confirmed_spend_since(self, since):
        if "spend" in self._raise_on:
            raise RuntimeError("db down")
        return self._spend

    def confirmed_buy_count(self):
        if "owned" in self._raise_on:
            raise RuntimeError("db down")
        return self._owned

    def recent_terminal_statuses(self, limit=50):
        if "statuses" in self._raise_on:
            raise RuntimeError("db down")
        return list(self._statuses)


# --------------------------------------------------------------------------- #
# Disabled limits
# --------------------------------------------------------------------------- #
def test_all_limits_disabled_allows_everything():
    cfg = make_config()  # all caps 0
    engine = RiskEngine(cfg, FakeRiskStore())
    orders = [make_buy(nft="A", price_usd=1000), make_offer(nft="B", price_usd=999)]
    decision = engine.evaluate(orders)
    assert decision.allowed == orders
    assert decision.blocked == []
    assert not decision.halted


def test_no_store_only_cycle_cap_binds():
    cfg = make_config(max_spend_per_cycle_usd=15)
    engine = RiskEngine(cfg)  # no store
    decision = engine.evaluate([make_buy(price_usd=10), make_buy(nft="B", price_usd=10)])
    assert len(decision.allowed) == 1
    assert len(decision.blocked) == 1


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_halts_all():
    cfg = make_config(max_consecutive_failures=3)
    store = FakeRiskStore(statuses=["failed", "failed", "failed"])
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1, market_usd=20)])
    assert decision.halted
    assert decision.allowed == []
    assert len(decision.blocked) == 1


def test_kill_switch_not_tripped_below_limit():
    cfg = make_config(max_consecutive_failures=3)
    store = FakeRiskStore(statuses=["failed", "failed"])
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1)])
    assert not decision.halted
    assert len(decision.allowed) == 1


def test_kill_switch_reset_by_success():
    cfg = make_config(max_consecutive_failures=2)
    store = FakeRiskStore(statuses=["failed", "confirmed", "failed"])
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1)])
    assert not decision.halted  # only 1 leading failure


def test_kill_switch_disabled_when_zero():
    cfg = make_config(max_consecutive_failures=0)
    store = FakeRiskStore(statuses=["failed"] * 50)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1)])
    assert not decision.halted


@pytest.mark.parametrize("statuses,expected", [
    ([], 0),
    (["failed"], 1),
    (["failed", "failed"], 2),
    (["failed", "confirmed", "failed"], 1),
    (["confirmed", "failed"], 0),
    (["cancelled"], 0),
    (["failed", "failed", "cancelled", "failed"], 2),
])
def test_leading_failures(statuses, expected):
    assert _leading_failures(statuses) == expected


# --------------------------------------------------------------------------- #
# Max open positions
# --------------------------------------------------------------------------- #
def test_open_positions_cap_blocks_excess():
    cfg = make_config(max_open_positions=2)
    store = FakeRiskStore(open_positions=1)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(nft="A", price_usd=1),
                                make_buy(nft="B", price_usd=1)])
    assert len(decision.allowed) == 1  # 1 existing + 1 new == cap of 2
    assert len(decision.blocked) == 1


def test_open_positions_cap_already_at_limit():
    cfg = make_config(max_open_positions=1)
    store = FakeRiskStore(open_positions=1)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1)])
    assert decision.allowed == []


# --------------------------------------------------------------------------- #
# Max owned cards (Feature 3)
# --------------------------------------------------------------------------- #
def test_owned_cards_cap_blocks_surplus_buy():
    cfg = make_config(max_owned_cards=2)
    store = FakeRiskStore(owned_cards=1)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(nft="A", price_usd=1),
                                make_buy(nft="B", price_usd=1)])
    assert len(decision.allowed) == 1  # 1 owned + 1 new == cap of 2
    assert len(decision.blocked) == 1
    assert "max owned cards" in decision.blocked[0][1]


def test_owned_cards_cap_already_at_limit():
    cfg = make_config(max_owned_cards=1)
    store = FakeRiskStore(owned_cards=1)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1)])
    assert decision.allowed == []


def test_owned_cards_cap_ignores_offers():
    # Offers do not transfer ownership yet, so they bypass the owned cap.
    cfg = make_config(max_owned_cards=1)
    store = FakeRiskStore(owned_cards=1)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_offer(nft="A", price_usd=1),
                                make_offer(nft="B", price_usd=1)])
    assert len(decision.allowed) == 2
    assert decision.blocked == []


def test_owned_cards_cap_disabled_when_zero():
    cfg = make_config(max_owned_cards=0)
    store = FakeRiskStore(owned_cards=100)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(nft="A", price_usd=1)])
    assert len(decision.allowed) == 1


def test_owned_cards_cap_cheapest_first_wins_headroom():
    # Plan order is cheapest-first; the cheaper buy takes the single slot.
    cfg = make_config(max_owned_cards=1)
    store = FakeRiskStore(owned_cards=0)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(nft="cheap", price_usd=5),
                                make_buy(nft="dear", price_usd=50)])
    assert [o.nft for o in decision.allowed] == ["cheap"]
    assert decision.blocked[0][0].nft == "dear"


def test_owned_cards_unreadable_halts_all():
    cfg = make_config(max_owned_cards=2)
    store = FakeRiskStore(raise_on={"owned"})
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1)])
    assert decision.halted
    assert decision.allowed == []


# --------------------------------------------------------------------------- #
# Spend caps
# --------------------------------------------------------------------------- #
def test_per_cycle_spend_cap():
    cfg = make_config(max_spend_per_cycle_usd=25)
    engine = RiskEngine(cfg, FakeRiskStore())
    decision = engine.evaluate([
        make_buy(nft="A", price_usd=10),
        make_buy(nft="B", price_usd=10),
        make_buy(nft="C", price_usd=10),  # would exceed 25
    ])
    assert len(decision.allowed) == 2
    assert len(decision.blocked) == 1


def test_daily_spend_cap_includes_history():
    cfg = make_config(max_spend_per_day_usd=100)
    store = FakeRiskStore(spend_today=95.0)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=10)])  # 95+10 > 100
    assert decision.allowed == []
    assert len(decision.blocked) == 1


def test_daily_spend_cap_allows_within():
    cfg = make_config(max_spend_per_day_usd=100)
    store = FakeRiskStore(spend_today=80.0)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=10)])
    assert len(decision.allowed) == 1


# --------------------------------------------------------------------------- #
# LIST exemption
# --------------------------------------------------------------------------- #
def test_list_orders_never_count_against_spend():
    cfg = make_config(max_spend_per_cycle_usd=5)
    engine = RiskEngine(cfg, FakeRiskStore())
    decision = engine.evaluate([make_list(nft="N", price_usd=1000, cycle_id="c")])
    assert len(decision.allowed) == 1


def test_list_orders_never_count_against_positions():
    cfg = make_config(max_open_positions=1)
    store = FakeRiskStore(open_positions=1)
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_list(nft="N", price_usd=10, cycle_id="c")])
    assert len(decision.allowed) == 1


# --------------------------------------------------------------------------- #
# Fail-safe
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raise_on", ["open", "spend", "statuses"])
def test_unreadable_state_halts_all(raise_on):
    cfg = make_config(max_open_positions=5, max_spend_per_day_usd=100,
                      max_consecutive_failures=3)
    store = FakeRiskStore(raise_on={raise_on})
    engine = RiskEngine(cfg, store)
    decision = engine.evaluate([make_buy(price_usd=1)])
    assert decision.halted
    assert decision.allowed == []


def test_evaluate_never_raises():
    cfg = make_config(max_open_positions=5)
    store = FakeRiskStore(raise_on={"open"})
    engine = RiskEngine(cfg, store)
    # Must return a decision, not raise.
    assert engine.evaluate([make_buy(price_usd=1)]).halted


# --------------------------------------------------------------------------- #
# Posture snapshot
# --------------------------------------------------------------------------- #
def test_posture_reports_limits():
    cfg = make_config(max_spend_per_cycle_usd=50, max_open_positions=3)
    engine = RiskEngine(cfg, FakeRiskStore())
    posture = engine.posture()
    assert posture["enabled"]
    assert posture["limits"]["max_spend_per_cycle_usd"] == 50
    assert posture["limits"]["max_open_positions"] == 3


def test_posture_disabled_when_no_limits():
    engine = RiskEngine(make_config(), FakeRiskStore())
    assert engine.posture()["enabled"] is False


def test_posture_reports_usage():
    cfg = make_config(max_spend_per_day_usd=100)
    store = FakeRiskStore(open_positions=2, spend_today=40.0, owned_cards=3)
    engine = RiskEngine(cfg, store)
    posture = engine.posture()
    assert posture["usage"]["open_positions"] == 2
    assert posture["usage"]["spend_today"] == 40.0
    assert posture["usage"]["owned_cards"] == 3


def test_posture_reports_owned_cap():
    cfg = make_config(max_owned_cards=5)
    engine = RiskEngine(cfg, FakeRiskStore())
    posture = engine.posture()
    assert posture["enabled"]
    assert posture["limits"]["max_owned_cards"] == 5


def test_blocked_orders_property():
    cfg = make_config(max_spend_per_cycle_usd=5)
    engine = RiskEngine(cfg, FakeRiskStore())
    decision = engine.evaluate([make_buy(price_usd=10)])
    assert len(decision.blocked_orders) == 1
    assert decision.blocked_orders[0].kind is OrderKind.BUY


def test_cycle_planned_spend_in_posture():
    cfg = make_config(max_spend_per_cycle_usd=100)
    engine = RiskEngine(cfg, FakeRiskStore())
    decision = engine.evaluate([make_buy(price_usd=10), make_offer(nft="B", price_usd=5)])
    assert decision.posture["cycle"]["planned_spend"] == 15.0
