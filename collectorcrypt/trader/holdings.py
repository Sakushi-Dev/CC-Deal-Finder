"""Holdings lifecycle — pure decision logic.

This module is the brain of the post-buy lifecycle (offer penetration, markdown,
offer-accept and the feature-5b market re-check). It contains **only** pure,
side-effect-free functions: every one takes the data it needs (a
:class:`~collectorcrypt.trader.store.Holding`, an
:class:`~collectorcrypt.trader.orders.Order`, a
:class:`~collectorcrypt.trader.config.TraderConfig`) plus an injected ``now``
timestamp, and returns a decision — it never reads the clock, the database, the
network or a wallet.

Why a separate pure module
--------------------------
* **Deterministic + trivially testable.** Frozen timestamps drive the markdown
  curve, the aging thresholds and the bump ceiling with no real clock, so the
  tests can never flake.
* **Failure default = no action.** Every predicate answers a conservative
  "should we act?" question; the I/O around it (Etappe 6) only acts when the
  answer is an unambiguous yes, so unreadable/edge state never moves money or
  lowers a price by accident.

The executor and engine (Etappe 5/6) wrap these decisions with the actual
persistence and the live/dry-run seam.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import TraderConfig
from .orders import Order, OrderKind, OrderStatus
from .store import Holding
from .strategy import resell_price

SECONDS_PER_HOUR = 3600.0
SECONDS_PER_DAY = 86400.0


def _days(value: float) -> float:
    return float(value) * SECONDS_PER_DAY


def _hours(value: float) -> float:
    return float(value) * SECONDS_PER_HOUR


# --------------------------------------------------------------------------- #
# Markdown curve (feature 5)
# --------------------------------------------------------------------------- #
def markdown_price(holding: Holding, cfg: TraderConfig, *,
                   step_jitter: float = 0.0) -> float:
    """Next markdown price for a held, listed card, clamped to the cost floor.

    Each step lowers the price by ``markdown_step_pct`` of the card's market
    value **at buy** (the persisted reference, which feature 5b may have
    raised), never below ``cost_usd`` — the permanent 0%-profit floor. The step
    size is anchored to ``market_usd_at_buy`` (not the live price) so the curve
    is stable and predictable regardless of how many steps have run.

    ``step_jitter`` is an optional factor in ``[-jitter, +jitter]`` (supplied by
    the engine from ``markdown_jitter_pct``) that scales this step's size so the
    markdown pattern cannot be reverse-engineered and waited out. 0 = no jitter.
    """
    floor = float(holding.cost_usd)
    current = holding.list_price_usd
    if current is None:
        current = holding.market_usd_at_buy
    step_pct = max(0.0, float(cfg.markdown_step_pct) * (1.0 + float(step_jitter)))
    step = holding.market_usd_at_buy * (step_pct / 100.0)
    return max(floor, float(current) - step)


def is_at_floor(holding: Holding) -> bool:
    """True when the listing has reached (or dropped to) the cost-basis floor."""
    if holding.list_price_usd is None:
        return False
    return float(holding.list_price_usd) <= float(holding.cost_usd)


def is_due_for_markdown(holding: Holding, cfg: TraderConfig, now: float, *,
                        interval_jitter: float = 0.0) -> bool:
    """Whether a listed, unsold card is due for its next markdown step.

    Returns ``False`` (no action) unless the card is listed, still unsold, still
    above the cost floor, has waited the initial delay since listing, and — for
    subsequent steps — has waited the inter-step interval since the last step.

    ``interval_jitter`` is an optional factor in ``[-jitter, +jitter]`` (from
    ``markdown_jitter_pct``) that scales both the initial delay and the
    inter-step interval so the timing is unpredictable. 0 = deterministic.
    """
    if holding.sold_at is not None or holding.listed_at is None:
        return False
    if is_at_floor(holding):
        return False
    scale = max(0.0, 1.0 + float(interval_jitter))
    # Initial delay measured from when the listing went live.
    if now - float(holding.listed_at) < _days(cfg.markdown_delay_days) * scale:
        return False
    # Subsequent steps respect the inter-step interval.
    if holding.last_markdown_at is not None:
        if now - float(holding.last_markdown_at) < _days(cfg.markdown_interval_days) * scale:
            return False
    return True


def markdown_change_is_meaningful(old_price: float, new_price: float,
                                  cfg: TraderConfig) -> bool:
    """Whether a markdown's price drop is large enough to justify the gas cost.

    ``markdown_min_change_usd == 0`` disables the guard (any drop is fine).
    Otherwise the drop (``old − new``) must be at least the configured minimum,
    so a few-cent adjustment never triggers an on-chain transaction that costs
    more in gas than it is worth.
    """
    min_change = max(0.0, float(cfg.markdown_min_change_usd))
    if min_change <= 0:
        return True
    return (float(old_price) - float(new_price)) >= min_change


def markdown_jitter_factor(key: str, jitter_pct: float) -> float:
    """Deterministic jitter factor in ``[-jitter_pct/100, +jitter_pct/100]``.

    Pure: the same ``key`` always yields the same factor (stable across cycles
    and processes — it uses SHA-256, not Python's salted ``hash``), so a given
    holding/step pair gets one fixed jittered interval and step size while the
    pattern varies unpredictably between cards and steps. ``jitter_pct <= 0``
    returns 0.0 (no jitter).
    """
    j = max(0.0, float(jitter_pct)) / 100.0
    if j <= 0:
        return 0.0
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
    return (unit * 2.0 - 1.0) * j  # [-j, +j]


# --------------------------------------------------------------------------- #
# Offer-accept stage (feature 5, after the floor)
# --------------------------------------------------------------------------- #
def is_due_for_offer_accept(holding: Holding, cfg: TraderConfig,
                            now: float) -> bool:
    """Whether a floored, unsold card is old enough to accept an incoming bid.

    Only once the listing has sat at the cost floor for
    ``offer_accept_delay_days`` may the best incoming offer be accepted. The
    floor-reached reference is the last markdown step that brought it down (or
    the listing time if it was listed at/below cost without any markdown).
    """
    if holding.sold_at is not None or holding.listed_at is None:
        return False
    if not is_at_floor(holding):
        return False
    floor_since = holding.last_markdown_at
    if floor_since is None:
        floor_since = holding.listed_at
    return now - float(floor_since) >= _days(cfg.offer_accept_delay_days)


def offer_meets_min_market(offer_usd: float, holding: Holding,
                           cfg: TraderConfig) -> bool:
    """Whether an incoming bid clears the configurable min-% of market value.

    ``offer_accept_min_market_pct == 0`` disables the gate (accept any). The
    market reference is the last re-checked value if available, otherwise the
    buy-time snapshot — the most current trustworthy figure (decision k).
    """
    min_pct = float(cfg.offer_accept_min_market_pct)
    if min_pct <= 0:
        return True
    market = holding.market_usd_current
    if market is None:
        market = holding.market_usd_at_buy
    threshold = float(market) * (min_pct / 100.0)
    return float(offer_usd) >= threshold


# --------------------------------------------------------------------------- #
# Incoming-offer reconstruction (from the card-activity feed)
# --------------------------------------------------------------------------- #
_OFFER_MADE = "Offer Made"
_OFFER_CLOSED = frozenset({"Offer Cancelled", "Offer Accepted"})


@dataclass(frozen=True)
class IncomingOffer:
    """The best still-open incoming bid reconstructed from the activity feed."""

    buyer: str
    amount: float


def _to_amount(value: object) -> float | None:
    try:
        amount = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def best_active_offer(feed: list[dict], *,
                      exclude_wallet: str = "") -> IncomingOffer | None:
    """Pick the highest still-open incoming bid from a card-activity feed.

    The CollectorCrypt card-activity endpoint returns a chronological log
    (newest first) that mixes offers, cancellations, accepts and listing edits;
    there is no standing-offers endpoint and no clean offer id. An offer is
    *active* only when the bidder's **most recent** offer-related event is an
    ``"Offer Made"`` that was not later cancelled or accepted.

    The feed is assumed newest-first, so the first offer-related event seen for
    a given bidder wallet is that wallet's current state. Among wallets whose
    current state is an open ``"Offer Made"``, the highest ``amount`` wins.
    Returns ``None`` when no bid is currently open. Pure and side-effect-free.

    ``exclude_wallet`` (e.g. our own address) drops that wallet's bids, so the
    caller can find the highest *competing* bid when deciding a dynamic price.
    """
    skip = (exclude_wallet or "").strip()
    state: dict[str, float | None] = {}
    for entry in feed or []:
        if not isinstance(entry, dict):
            continue
        action = entry.get("action")
        if action != _OFFER_MADE and action not in _OFFER_CLOSED:
            continue
        sender = entry.get("from") or {}
        wallet = str(sender.get("wallet") or "").strip()
        if not wallet or wallet in state:
            continue  # already have this wallet's newest offer-event
        if skip and wallet == skip:
            continue  # ignore our own bids when looking for competitors
        if action == _OFFER_MADE:
            state[wallet] = _to_amount(entry.get("amount"))
        else:  # cancelled / accepted -> the wallet has no open offer
            state[wallet] = None
    best: IncomingOffer | None = None
    for wallet, amount in state.items():
        if amount is None:
            continue
        if best is None or amount > best.amount:
            best = IncomingOffer(buyer=wallet, amount=amount)
    return best


# --------------------------------------------------------------------------- #
# Unpopular blacklist (feature 4)
# --------------------------------------------------------------------------- #
def should_blacklist(holding: Holding, cfg: TraderConfig, now: float) -> bool:
    """Whether a held, listed-but-unsold card should be flagged unpopular.

    A card that has sat listed and unsold for ``unpopular_days`` is flagged so
    it is never **re-acquired**; it does not stop us selling the one we hold.
    Already-blacklisted or sold cards are skipped.
    """
    if holding.blacklisted or holding.sold_at is not None:
        return False
    if holding.listed_at is None:
        return False
    return now - float(holding.listed_at) >= _days(cfg.unpopular_days)


# --------------------------------------------------------------------------- #
# Offer penetration (feature 1)
# --------------------------------------------------------------------------- #
def _offer_age_reference(order: Order) -> float:
    """The timestamp an open offer's age is measured from.

    After a bump, age is measured from the last bump; before the first bump it
    is measured from when the order was created (placed).
    """
    if order.bump_count > 0 and order.last_bump_at > 0:
        return order.last_bump_at
    return order.created_at


def _is_open_offer(order: Order) -> bool:
    return order.kind is OrderKind.OFFER and order.status is OrderStatus.OPEN


def should_bump(order: Order, cfg: TraderConfig, now: float) -> bool:
    """Whether an aged open offer should be bumped to re-trigger a notification.

    True only for an open offer that still has bumps left
    (``bump_count < offer_bump_max``) and has been idle for at least
    ``offer_bump_age_hours`` since it was placed or last bumped.
    """
    if not _is_open_offer(order):
        return False
    if order.bump_count >= int(cfg.offer_bump_max):
        return False
    return now - _offer_age_reference(order) >= _hours(cfg.offer_bump_age_hours)


def next_bump_price(order: Order, cfg: TraderConfig) -> float:
    """The bid price after one bump (current price + the configured increment)."""
    return float(order.price_usd) + float(cfg.offer_bump_usd)


def should_cancel_offer(order: Order, cfg: TraderConfig, now: float) -> bool:
    """Whether an open offer with its bumps exhausted should be cancelled.

    Once an offer has been bumped the maximum number of times and has aged a
    further full interval with no fill, it is cancelled (escrow refunds).
    """
    if not _is_open_offer(order):
        return False
    if order.bump_count < int(cfg.offer_bump_max):
        return False
    return now - _offer_age_reference(order) >= _hours(cfg.offer_bump_age_hours)


# --------------------------------------------------------------------------- #
# Market re-check (feature 5b)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecheckDecision:
    """The outcome of a held-card market re-check (feature 5b).

    ``raised`` is the only branch that mutates the sell cycle: on a market rise
    the resale target is raised, the sell timers are reset to day 0, **and the
    stored ``market_usd_at_buy`` is overwritten** with the new value. On a flat
    or falling market nothing changes (the markdown cycle continues), so
    ``new_market_usd_at_buy`` simply echoes the existing reference.

    Either way the caller always records ``market_usd_current`` /
    ``market_checked_at``; this struct only governs the raise + reset.
    """

    raised: bool
    new_market_usd_at_buy: float
    new_list_price: float


def is_due_for_recheck(holding: Holding, cfg: TraderConfig, now: float) -> bool:
    """Whether a held, unsold card's market value should be re-checked now."""
    if holding.sold_at is not None:
        return False
    if holding.market_checked_at is None:
        return True
    return now - float(holding.market_checked_at) >= _hours(cfg.market_recheck_hours)


def recheck_decision(holding: Holding, current_market: float,
                     cfg: TraderConfig) -> RecheckDecision:
    """Decide what a re-check at ``current_market`` changes (feature 5b).

    **Positive** (``current_market > market_usd_at_buy``): raise the resale
    target to the new market-based price, signal a sell-cycle reset, and report
    the new reference to store as ``market_usd_at_buy``.

    **Flat / negative**: no change — the markdown cycle continues and the old
    reference is kept.

    ⚠️ The caller **must** persist ``new_market_usd_at_buy`` on a raise (not just
    compare): keeping the old, lower reference would make every later re-check
    see "positive" again and reset the sell cycle forever, so the card would be
    held indefinitely and never sell. Overwriting means a reset only recurs if
    the market rises *further*. The markdown floor stays pinned to ``cost_usd``,
    so a raised reference never lets the price fall below cost.
    """
    if float(current_market) > float(holding.market_usd_at_buy):
        return RecheckDecision(
            raised=True,
            new_market_usd_at_buy=float(current_market),
            new_list_price=resell_price(float(current_market), cfg),
        )
    keep_price = holding.list_price_usd
    if keep_price is None:
        keep_price = holding.market_usd_at_buy
    return RecheckDecision(
        raised=False,
        new_market_usd_at_buy=float(holding.market_usd_at_buy),
        new_list_price=float(keep_price),
    )
