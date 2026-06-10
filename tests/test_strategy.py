"""Pure strategy decision-logic tests.

Covers the order-book-aware dynamic range bidding helpers added for the
escrow-leak fix. Both functions are pure (no I/O), so the order book is passed
in directly as the highest competing bid and a hard price cap.
"""
from __future__ import annotations

from collectorcrypt.trader.strategy import (dynamic_bidding_enabled,
                                            dynamic_offer_bid)

from .conftest import make_config


# --------------------------------------------------------------------------- #
# dynamic_bidding_enabled — opt-in gate
# --------------------------------------------------------------------------- #
def test_dynamic_bidding_disabled_by_default():
    assert dynamic_bidding_enabled(make_config()) is False


def test_dynamic_bidding_requires_both_open_and_ceiling():
    assert dynamic_bidding_enabled(
        make_config(offer_open_discount_pct=30.0, offer_ceiling_pct=0.0)) is False
    assert dynamic_bidding_enabled(
        make_config(offer_open_discount_pct=0.0, offer_ceiling_pct=10.0)) is False
    assert dynamic_bidding_enabled(
        make_config(offer_open_discount_pct=30.0, offer_ceiling_pct=10.0)) is True


# --------------------------------------------------------------------------- #
# dynamic_offer_bid — open / outbid / skip branches
# open_discount=30 -> open_price=70 ; ceiling=10 -> ceiling_price=90 (ask=100)
# --------------------------------------------------------------------------- #
def _cfg(**kw):
    base = dict(offer_open_discount_pct=30.0, offer_ceiling_pct=10.0,
                offer_increment_usd=0.01)
    base.update(kw)
    return make_config(**base)


def test_dynamic_bid_uncontested_uses_opening_lowball():
    # No competing bid -> bid our opening lowball (70), not the ceiling.
    price = dynamic_offer_bid(100.0, None, _cfg(), max_price=1000.0)
    assert price == 70.0


def test_dynamic_bid_below_open_still_uses_open():
    # Competitor below our opening lowball -> no need to pay more than 70.
    price = dynamic_offer_bid(100.0, 50.0, _cfg(), max_price=1000.0)
    assert price == 70.0


def test_dynamic_bid_in_range_outbids_competitor():
    # Competitor inside [open, ceiling] -> just outbid by the increment.
    price = dynamic_offer_bid(100.0, 85.0, _cfg(), max_price=1000.0)
    assert price == 85.01


def test_dynamic_bid_above_ceiling_skips():
    # Winning would need a bid above the ceiling (90) -> skip (None).
    assert dynamic_offer_bid(100.0, 90.0, _cfg(), max_price=1000.0) is None
    assert dynamic_offer_bid(100.0, 95.0, _cfg(), max_price=1000.0) is None


def test_dynamic_bid_increment_breaching_ceiling_skips():
    # A large increment that would push the outbid past the ceiling -> skip.
    cfg = _cfg(offer_increment_usd=5.0)
    assert dynamic_offer_bid(100.0, 89.0, cfg, max_price=1000.0) is None


def test_dynamic_bid_budget_below_open_skips():
    # Remaining budget/cap cannot even cover the opening lowball -> skip.
    assert dynamic_offer_bid(100.0, None, _cfg(), max_price=60.0) is None


def test_dynamic_bid_budget_caps_outbid():
    # Cap (from budget) below the would-be outbid -> skip rather than overpay.
    # open=70, competitor=85 -> outbid 85.01, but cap is 80 -> None.
    assert dynamic_offer_bid(100.0, 85.0, _cfg(), max_price=80.0) is None


def test_dynamic_bid_zero_ask_skips():
    assert dynamic_offer_bid(0.0, None, _cfg(), max_price=1000.0) is None
