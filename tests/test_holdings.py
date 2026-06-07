"""Pure holdings-lifecycle decision-logic tests (Etappe 3).

Every function in ``trader/holdings.py`` is pure and takes an injected ``now``,
so these tests drive the markdown curve, aging thresholds, bump ceiling,
blacklist trigger and the feature-5b re-check with frozen timestamps — no real
clock, no I/O, no flakiness.
"""
from __future__ import annotations

from collectorcrypt.trader import holdings as H
from collectorcrypt.trader.holdings import (RecheckDecision, is_at_floor,
                                            is_due_for_markdown,
                                            is_due_for_offer_accept,
                                            is_due_for_recheck, markdown_price,
                                            next_bump_price,
                                            offer_meets_min_market,
                                            recheck_decision, should_blacklist,
                                            should_bump, should_cancel_offer)
from collectorcrypt.trader.orders import OrderStatus
from collectorcrypt.trader.store import Holding

from .conftest import make_config, make_offer

# A fixed reference epoch so every "now" is explicit and deterministic.
T0 = 1_000_000.0
DAY = H.SECONDS_PER_DAY
HOUR = H.SECONDS_PER_HOUR


def _holding(**kw) -> Holding:
    base = dict(nft="H1", name="Card", category="Pokemon", acquired_at=T0,
                cost_usd=10.0, market_usd_at_buy=20.0, status="held")
    base.update(kw)
    return Holding(**base)


def _open_offer(**kw):
    o = make_offer(nft="N", cycle_id="c", **kw)
    o.transition(OrderStatus.OPEN)
    return o


# --------------------------------------------------------------------------- #
# markdown_price — step size + cost floor
# --------------------------------------------------------------------------- #
def test_markdown_price_one_step():
    h = _holding(market_usd_at_buy=100.0, cost_usd=50.0, list_price_usd=90.0)
    cfg = make_config(markdown_step_pct=1.0)
    # step = 100 * 1% = 1.0 -> 90 - 1 = 89
    assert markdown_price(h, cfg) == 89.0


def test_markdown_price_clamped_to_cost_floor():
    h = _holding(market_usd_at_buy=100.0, cost_usd=50.0, list_price_usd=50.5)
    cfg = make_config(markdown_step_pct=1.0)  # would go to 49.5
    assert markdown_price(h, cfg) == 50.0  # never below cost


def test_markdown_price_falls_back_to_market_when_unlisted():
    h = _holding(market_usd_at_buy=100.0, cost_usd=50.0, list_price_usd=None)
    cfg = make_config(markdown_step_pct=2.0)
    # base = market_usd_at_buy(100) - 2 = 98
    assert markdown_price(h, cfg) == 98.0


def test_markdown_step_uses_buy_reference_not_live_price():
    # Step size anchored to market_usd_at_buy (100), independent of list price.
    h = _holding(market_usd_at_buy=100.0, cost_usd=10.0, list_price_usd=60.0)
    cfg = make_config(markdown_step_pct=5.0)  # 5 of 100 = 5
    assert markdown_price(h, cfg) == 55.0


# --------------------------------------------------------------------------- #
# is_at_floor
# --------------------------------------------------------------------------- #
def test_is_at_floor_true_at_or_below_cost():
    assert is_at_floor(_holding(cost_usd=10.0, list_price_usd=10.0)) is True
    assert is_at_floor(_holding(cost_usd=10.0, list_price_usd=9.0)) is True


def test_is_at_floor_false_above_cost_or_unlisted():
    assert is_at_floor(_holding(cost_usd=10.0, list_price_usd=11.0)) is False
    assert is_at_floor(_holding(cost_usd=10.0, list_price_usd=None)) is False


# --------------------------------------------------------------------------- #
# is_due_for_markdown — delay, interval, floor, sold/unlisted guards
# --------------------------------------------------------------------------- #
def test_markdown_not_due_before_delay():
    h = _holding(listed_at=T0, list_price_usd=20.0)
    cfg = make_config(markdown_delay_days=3.0)
    assert is_due_for_markdown(h, cfg, now=T0 + 2 * DAY) is False


def test_markdown_due_after_delay():
    h = _holding(listed_at=T0, list_price_usd=20.0)
    cfg = make_config(markdown_delay_days=3.0)
    assert is_due_for_markdown(h, cfg, now=T0 + 3 * DAY) is True


def test_markdown_respects_interval_between_steps():
    h = _holding(listed_at=T0, list_price_usd=18.0, last_markdown_at=T0 + 3 * DAY,
                 markdown_steps=1)
    cfg = make_config(markdown_delay_days=3.0, markdown_interval_days=3.0)
    # Only 2 days since last step -> not due.
    assert is_due_for_markdown(h, cfg, now=T0 + 5 * DAY) is False
    # 3 days since last step -> due.
    assert is_due_for_markdown(h, cfg, now=T0 + 6 * DAY) is True


def test_markdown_not_due_at_floor():
    h = _holding(listed_at=T0, cost_usd=10.0, list_price_usd=10.0)
    cfg = make_config(markdown_delay_days=0.0)
    assert is_due_for_markdown(h, cfg, now=T0 + 10 * DAY) is False


def test_markdown_not_due_when_sold_or_unlisted():
    cfg = make_config(markdown_delay_days=0.0)
    sold = _holding(listed_at=T0, list_price_usd=20.0, sold_at=T0 + DAY)
    unlisted = _holding(listed_at=None, list_price_usd=20.0)
    assert is_due_for_markdown(sold, cfg, now=T0 + 10 * DAY) is False
    assert is_due_for_markdown(unlisted, cfg, now=T0 + 10 * DAY) is False


# --------------------------------------------------------------------------- #
# is_due_for_offer_accept
# --------------------------------------------------------------------------- #
def test_offer_accept_not_due_above_floor():
    h = _holding(listed_at=T0, cost_usd=10.0, list_price_usd=15.0)
    cfg = make_config(offer_accept_delay_days=3.0)
    assert is_due_for_offer_accept(h, cfg, now=T0 + 100 * DAY) is False


def test_offer_accept_due_after_floor_delay():
    h = _holding(listed_at=T0, cost_usd=10.0, list_price_usd=10.0,
                 last_markdown_at=T0 + 5 * DAY)
    cfg = make_config(offer_accept_delay_days=3.0)
    assert is_due_for_offer_accept(h, cfg, now=T0 + 8 * DAY - 1) is False
    assert is_due_for_offer_accept(h, cfg, now=T0 + 8 * DAY) is True


def test_offer_accept_uses_listed_at_when_no_markdown():
    # Listed already at/below cost, no markdown step recorded.
    h = _holding(listed_at=T0, cost_usd=10.0, list_price_usd=10.0,
                 last_markdown_at=None)
    cfg = make_config(offer_accept_delay_days=2.0)
    assert is_due_for_offer_accept(h, cfg, now=T0 + 2 * DAY) is True


# --------------------------------------------------------------------------- #
# offer_meets_min_market
# --------------------------------------------------------------------------- #
def test_offer_min_market_disabled_accepts_any():
    h = _holding(market_usd_at_buy=100.0)
    cfg = make_config(offer_accept_min_market_pct=0.0)
    assert offer_meets_min_market(1.0, h, cfg) is True


def test_offer_min_market_threshold():
    h = _holding(market_usd_at_buy=100.0, market_usd_current=None)
    cfg = make_config(offer_accept_min_market_pct=80.0)
    assert offer_meets_min_market(79.0, h, cfg) is False
    assert offer_meets_min_market(80.0, h, cfg) is True


def test_offer_min_market_prefers_current_value():
    h = _holding(market_usd_at_buy=100.0, market_usd_current=50.0)
    cfg = make_config(offer_accept_min_market_pct=80.0)
    # threshold = 50 * 80% = 40, not 80
    assert offer_meets_min_market(40.0, h, cfg) is True


# --------------------------------------------------------------------------- #
# should_blacklist
# --------------------------------------------------------------------------- #
def test_should_blacklist_after_unpopular_days():
    h = _holding(listed_at=T0)
    cfg = make_config(unpopular_days=7.0)
    assert should_blacklist(h, cfg, now=T0 + 7 * DAY - 1) is False
    assert should_blacklist(h, cfg, now=T0 + 7 * DAY) is True


def test_should_blacklist_skips_already_flagged_and_sold():
    cfg = make_config(unpopular_days=1.0)
    flagged = _holding(listed_at=T0, blacklisted=True)
    sold = _holding(listed_at=T0, sold_at=T0 + DAY)
    unlisted = _holding(listed_at=None)
    assert should_blacklist(flagged, cfg, now=T0 + 30 * DAY) is False
    assert should_blacklist(sold, cfg, now=T0 + 30 * DAY) is False
    assert should_blacklist(unlisted, cfg, now=T0 + 30 * DAY) is False


# --------------------------------------------------------------------------- #
# should_bump / next_bump_price
# --------------------------------------------------------------------------- #
def test_should_bump_after_age_from_creation():
    o = _open_offer(price_usd=5.0)
    o.created_at = T0
    cfg = make_config(offer_bump_age_hours=24.0, offer_bump_max=3)
    assert should_bump(o, cfg, now=T0 + 23 * HOUR) is False
    assert should_bump(o, cfg, now=T0 + 24 * HOUR) is True


def test_should_bump_measures_from_last_bump():
    o = _open_offer(price_usd=5.0)
    o.created_at = T0
    o.bump_count = 1
    o.last_bump_at = T0 + 24 * HOUR
    cfg = make_config(offer_bump_age_hours=24.0, offer_bump_max=3)
    assert should_bump(o, cfg, now=T0 + 47 * HOUR) is False
    assert should_bump(o, cfg, now=T0 + 48 * HOUR) is True


def test_should_bump_stops_at_max():
    o = _open_offer(price_usd=5.0)
    o.created_at = T0
    o.bump_count = 3
    o.last_bump_at = T0
    cfg = make_config(offer_bump_age_hours=1.0, offer_bump_max=3)
    assert should_bump(o, cfg, now=T0 + 100 * HOUR) is False


def test_should_bump_only_open_offers():
    cfg = make_config(offer_bump_age_hours=0.0, offer_bump_max=3)
    planned = make_offer(nft="N", cycle_id="c")  # still PLANNED, not OPEN
    assert should_bump(planned, cfg, now=T0 + 100 * HOUR) is False


def test_next_bump_price_adds_increment():
    o = _open_offer(price_usd=5.0)
    cfg = make_config(offer_bump_usd=0.10)
    assert round(next_bump_price(o, cfg), 2) == 5.10


# --------------------------------------------------------------------------- #
# should_cancel_offer
# --------------------------------------------------------------------------- #
def test_should_cancel_after_bumps_exhausted_and_aged():
    o = _open_offer(price_usd=5.0)
    o.bump_count = 3
    o.last_bump_at = T0
    cfg = make_config(offer_bump_age_hours=24.0, offer_bump_max=3)
    assert should_cancel_offer(o, cfg, now=T0 + 23 * HOUR) is False
    assert should_cancel_offer(o, cfg, now=T0 + 24 * HOUR) is True


def test_should_not_cancel_before_bumps_exhausted():
    o = _open_offer(price_usd=5.0)
    o.bump_count = 1
    o.last_bump_at = T0
    cfg = make_config(offer_bump_age_hours=1.0, offer_bump_max=3)
    assert should_cancel_offer(o, cfg, now=T0 + 100 * HOUR) is False


# --------------------------------------------------------------------------- #
# is_due_for_recheck
# --------------------------------------------------------------------------- #
def test_recheck_due_when_never_checked():
    h = _holding(market_checked_at=None)
    cfg = make_config(market_recheck_hours=24.0)
    assert is_due_for_recheck(h, cfg, now=T0) is True


def test_recheck_respects_interval():
    h = _holding(market_checked_at=T0)
    cfg = make_config(market_recheck_hours=24.0)
    assert is_due_for_recheck(h, cfg, now=T0 + 23 * HOUR) is False
    assert is_due_for_recheck(h, cfg, now=T0 + 24 * HOUR) is True


def test_recheck_not_due_when_sold():
    h = _holding(market_checked_at=None, sold_at=T0)
    cfg = make_config(market_recheck_hours=24.0)
    assert is_due_for_recheck(h, cfg, now=T0 + 100 * HOUR) is False


# --------------------------------------------------------------------------- #
# recheck_decision — feature 5b (positive raise + no-infinite-reset)
# --------------------------------------------------------------------------- #
def test_recheck_positive_raises_and_resets():
    h = _holding(market_usd_at_buy=100.0, list_price_usd=90.0)
    cfg = make_config(resell_discount_pct=10.0)
    dec = recheck_decision(h, current_market=120.0, cfg=cfg)
    assert dec.raised is True
    assert dec.new_market_usd_at_buy == 120.0
    # new resale target = 120 * (1 - 10%) = 108
    assert dec.new_list_price == 108.0


def test_recheck_flat_or_negative_no_change():
    h = _holding(market_usd_at_buy=100.0, list_price_usd=90.0)
    cfg = make_config(resell_discount_pct=10.0)
    flat = recheck_decision(h, current_market=100.0, cfg=cfg)
    down = recheck_decision(h, current_market=80.0, cfg=cfg)
    assert flat.raised is False
    assert flat.new_market_usd_at_buy == 100.0
    assert flat.new_list_price == 90.0
    assert down.raised is False
    assert down.new_market_usd_at_buy == 100.0


def test_recheck_no_infinite_reset_property():
    """After a raise, a second re-check at the SAME market must NOT raise again.

    This is the critical §5b invariant: the caller overwrites
    ``market_usd_at_buy`` with the raised value, so the next re-check compares
    against the new (higher) reference and only resets if the market rises
    further. Simulate that here.
    """
    cfg = make_config(resell_discount_pct=10.0)
    h = _holding(market_usd_at_buy=100.0, list_price_usd=90.0)
    first = recheck_decision(h, current_market=120.0, cfg=cfg)
    assert first.raised is True
    # Caller persists the raised reference; emulate the updated holding.
    raised = _holding(market_usd_at_buy=first.new_market_usd_at_buy,
                      list_price_usd=first.new_list_price)
    second = recheck_decision(raised, current_market=120.0, cfg=cfg)
    assert second.raised is False  # no further rise -> no second reset


def test_recheck_raises_again_only_if_market_rises_further():
    cfg = make_config(resell_discount_pct=10.0)
    raised = _holding(market_usd_at_buy=120.0, list_price_usd=108.0)
    dec = recheck_decision(raised, current_market=130.0, cfg=cfg)
    assert dec.raised is True
    assert dec.new_market_usd_at_buy == 130.0
