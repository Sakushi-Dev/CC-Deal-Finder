"""Buy strategy — pure, side-effect-free decision logic.

Given a set of normalized CC cards and the available USDC volume, produce a
ranked :class:`BuyPlan`. Design goals from the spec:

* **CC only** — listings from other marketplaces (e.g. Magic Eden / ``ME``)
  are ignored.
* **Quantity over quality** — buy the cheapest qualifying cards first to
  accumulate as many as possible within budget.
* **Escalation protocol** — when the available volume is large enough, the
  per-card price cap is raised so expensive cards become eligible too.

This module knows nothing about HTTP, wallets or signing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..normalize import to_usd
from .config import TraderConfig


@dataclass
class Candidate:
    """A single qualifying listing with its computed economics."""

    card: dict[str, Any]
    ask_usd: float
    market_usd: float
    discount_pct: float
    resell_usd: float = 0.0

    @property
    def nft(self) -> str:
        return self.card.get("nft", "")

    @property
    def name(self) -> str:
        return self.card.get("name", "")


@dataclass
class Offer:
    """A standing buy order (bid) placed below a listing's ask price."""

    candidate: Candidate
    offer_usd: float

    @property
    def nft(self) -> str:
        return self.candidate.nft

    @property
    def name(self) -> str:
        return self.candidate.name


@dataclass
class BuyPlan:
    """The bot's intended purchases for one cycle."""

    items: list[Candidate] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    skipped: int = 0
    available_volume: float = 0.0
    direct_budget: float = 0.0
    offer_budget: float = 0.0
    card_cap_usd: float = 0.0
    escalated: bool = False

    @property
    def total_cost(self) -> float:
        return sum(item.ask_usd for item in self.items)

    @property
    def offer_cost(self) -> float:
        return sum(o.offer_usd for o in self.offers)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def offer_count(self) -> int:
        return len(self.offers)

    @property
    def remaining_volume(self) -> float:
        return max(0.0, self.available_volume - self.total_cost - self.offer_cost)

    @property
    def resell_value(self) -> float:
        """Total list price of all bought cards once relisted for sale."""
        return sum(item.resell_usd for item in self.items)

    @property
    def resell_profit(self) -> float:
        """Realistic profit on direct buys: resale price minus what we paid."""
        return sum(item.resell_usd - item.ask_usd for item in self.items)

    @property
    def offer_resell_profit(self) -> float:
        """Realistic profit on offers if filled, then relisted for sale."""
        return sum(o.candidate.resell_usd - o.offer_usd for o in self.offers)


def effective_card_cap(available_volume: float,
                       cfg: TraderConfig) -> tuple[float, bool]:
    """Return ``(per_card_cap, escalated)``.

    Escalation kicks in once the available volume reaches the configured
    threshold, raising the cap so expensive cards can be bought as well.
    """
    if available_volume >= cfg.escalation_volume_usd:
        return max(cfg.base_max_card_usd, cfg.escalation_max_card_usd), True
    return cfg.base_max_card_usd, False


def resell_price(market_usd: float, cfg: TraderConfig) -> float:
    """Target relist price for a card we own.

    The bot buys low and resells **below market value** so the listing stays
    attractive, while still locking in a margin. Example: buy at -30%, relist
    at ``TRADER_RESELL_DISCOUNT_PCT`` (e.g. -10%) of the insured market value.
    """
    factor = max(0.0, 1.0 - cfg.resell_discount_pct / 100.0)
    return market_usd * factor


def _economics(card: dict[str, Any], sol_rate: float,
               allowed: set[str]) -> Candidate | None:
    """Build a :class:`Candidate` with computed economics, or ``None``.

    Applies only the universal validity rules (CC-only marketplace, a usable
    ask price and a positive insured/market value). Rule-specific filters such
    as the minimum discount are left to the callers.
    """
    if (card.get("marketplace") or "").upper() not in allowed:
        return None
    ask_usd = to_usd(card.get("price_raw"), card.get("currency") or "", sol_rate)
    if ask_usd is None or ask_usd <= 0:
        return None
    market_usd = card.get("insured_value")
    if not market_usd or market_usd <= 0:
        return None
    discount_pct = (market_usd - ask_usd) / market_usd * 100.0
    return Candidate(card=card, ask_usd=ask_usd,
                     market_usd=float(market_usd), discount_pct=discount_pct)


def make_candidates(cards: list[dict[str, Any]], sol_rate: float,
                    cfg: TraderConfig,
                    blacklist: set[str] | None = None) -> list[Candidate]:
    """Filter normalized cards down to qualifying **direct-buy** candidates.

    ``blacklist`` is the set of NFT addresses flagged unpopular (Feature 4); any
    card in it is dropped so the bot never re-acquires a card it already
    struggled to sell.
    """
    allowed = {m.upper() for m in cfg.allowed_marketplaces}
    out: list[Candidate] = []
    for card in cards:
        cand = _economics(card, sol_rate, allowed)
        if cand is None:
            continue
        if blacklist and cand.nft in blacklist:
            continue
        if cand.discount_pct < cfg.min_discount_pct:
            continue
        out.append(cand)
    return out


def make_offer_candidates(cards: list[dict[str, Any]], sol_rate: float,
                          cfg: TraderConfig,
                          blacklist: set[str] | None = None) -> list[Candidate]:
    """Filter cards down to candidates we may place a **bid** (offer) on.

    Offers bid *below* the ask, so a listing need not already meet the minimum
    discount to be worth an offer. However, if the ask sits well above market
    value the seller is expecting a profit and is unlikely to accept a lowball
    bid, so listings priced more than ``TRADER_OFFER_MAX_PREMIUM_PCT`` above
    the insured market value are excluded.

    ``blacklist`` (Feature 4) drops unpopular NFTs from the offer pool too, so
    the unpopular filter covers both buys and bids.
    """
    allowed = {m.upper() for m in cfg.allowed_marketplaces}
    max_ratio = 1.0 + max(0.0, cfg.offer_max_premium_pct) / 100.0
    out: list[Candidate] = []
    for card in cards:
        cand = _economics(card, sol_rate, allowed)
        if cand is None:
            continue
        if blacklist and cand.nft in blacklist:
            continue
        if cand.ask_usd > cand.market_usd * max_ratio:
            continue
        out.append(cand)
    return out


def diagnose_listings(cards: list[dict[str, Any]], sol_rate: float,
                      cfg: TraderConfig, cap: float,
                      limit: int = 12) -> list[dict[str, Any]]:
    """Explain the closest deals on the market, qualifying or not.

    Returns the best CC listings (ranked by discount) annotated with whether
    they pass each rule and a human-readable reason. This powers the demo /
    market-scan view so the user always sees *which hypothetical cards* are
    near the buy threshold, even when nothing actually qualifies.
    """
    allowed = {m.upper() for m in cfg.allowed_marketplaces}
    rows: list[dict[str, Any]] = []
    for card in cards:
        if (card.get("marketplace") or "").upper() not in allowed:
            continue
        ask = to_usd(card.get("price_raw"), card.get("currency") or "", sol_rate)
        if ask is None or ask <= 0:
            continue
        market = card.get("insured_value")
        if not market or market <= 0:
            continue
        market = float(market)
        discount = (market - ask) / market * 100.0
        resale = resell_price(market, cfg)

        if cfg.min_card_usd > 0 and market < cfg.min_card_usd:
            ok, reason = False, (
                f"value ${market:.0f} < min ${cfg.min_card_usd:.0f}"
            )
        elif discount < cfg.min_discount_pct:
            ok, reason = False, (
                f"discount {discount:.1f}% < min {cfg.min_discount_pct:.0f}%"
            )
        elif ask > cap:
            ok, reason = False, f"price ${ask:.0f} > cap ${cap:.0f}"
        elif resale <= ask:
            ok, reason = False, "no resale margin"
        else:
            ok, reason = True, "qualifies"

        rows.append({
            "name": card.get("name", ""),
            "nft": card.get("nft", ""),
            "category": card.get("category", ""),
            "ask_usd": round(ask, 2),
            "market_usd": round(market, 2),
            "discount_pct": round(discount, 1),
            "resell_usd": round(resale, 2),
            "qualifies": ok,
            "reason": reason,
        })

    rows.sort(key=lambda r: r["discount_pct"], reverse=True)
    return rows[:limit]


def split_volume(available_volume: float,
                 cfg: TraderConfig) -> tuple[float, float]:
    """Split the available volume into ``(direct_budget, offer_budget)``.

    The two percentages are taken from the config. If they sum to more than
    100, they are scaled down proportionally so the total never exceeds the
    available volume.
    """
    direct_pct = max(0.0, cfg.direct_buy_pct)
    offer_pct = max(0.0, cfg.offer_pct)
    total = direct_pct + offer_pct
    if total > 100.0 and total > 0:
        direct_pct = direct_pct / total * 100.0
        offer_pct = offer_pct / total * 100.0
    direct_budget = available_volume * direct_pct / 100.0
    offer_budget = available_volume * offer_pct / 100.0
    return direct_budget, offer_budget


def build_plan(candidates: list[Candidate], available_volume: float,
               cfg: TraderConfig,
               offer_candidates: list[Candidate] | None = None) -> BuyPlan:
    """Greedy, quantity-first allocation of the available volume.

    The volume is first split into a direct-buy budget and an offer budget.
    Within each budget the cheapest cards are taken first (maximising count).
    Direct buys pay the ask; offers bid ``TRADER_OFFER_DISCOUNT_PCT`` below it.
    The per-card cap is raised automatically once the escalation threshold is
    reached. ``offer_candidates`` is the (typically broader) pool used for the
    offer stage; it defaults to ``candidates`` when not supplied.
    """
    cap, escalated = effective_card_cap(available_volume, cfg)
    direct_budget, offer_budget = split_volume(available_volume, cfg)
    plan = BuyPlan(available_volume=available_volume, card_cap_usd=cap,
                   escalated=escalated, direct_budget=direct_budget,
                   offer_budget=offer_budget)

    # Cheapest first -> most cards per dollar (quantity over quality).
    ordered = sorted(candidates, key=lambda c: c.ask_usd)
    ordered_offers = sorted(offer_candidates if offer_candidates is not None
                            else candidates, key=lambda c: c.ask_usd)
    offer_factor = max(0.0, 1.0 - cfg.offer_discount_pct / 100.0)
    floor = max(0.0, cfg.min_card_usd)
    taken: set[str] = set()

    # 1) Direct buys at the ask price.
    direct_left = direct_budget
    for cand in ordered:
        # Value floor: skip low-value cards — valuable cards resell better and
        # carry less liquidity risk. Measured against insured/market value, so
        # a cheap ask on a valuable card still qualifies.
        if cand.market_usd < floor:
            plan.skipped += 1
            continue
        if cand.ask_usd > cap:
            plan.skipped += 1
            continue
        # Sell rule: only buy if we can relist above what we pay (guaranteed
        # profit). Resale price = market * (1 - resell_discount_pct).
        resale = resell_price(cand.market_usd, cfg)
        if resale <= cand.ask_usd:
            plan.skipped += 1
            continue
        if cand.ask_usd > direct_left:
            continue
        # Copy rather than mutate the input candidate so build_plan stays
        # side-effect-free (callers may reuse the same candidate list).
        plan.items.append(replace(cand, resell_usd=resale))
        direct_left -= cand.ask_usd
        taken.add(cand.nft)

    # 2) Offers below the ask, from the remaining (offer) budget.
    offer_left = offer_budget
    for cand in ordered_offers:
        if cand.nft in taken:
            continue
        if cand.market_usd < floor:
            continue
        bid = cand.ask_usd * offer_factor
        if bid <= 0 or bid > cap or bid > offer_left:
            continue
        # Same sell rule applied to the bid price.
        resale = resell_price(cand.market_usd, cfg)
        if resale <= bid:
            continue
        # Copy rather than mutate the input candidate (see direct-buy stage).
        plan.offers.append(
            Offer(candidate=replace(cand, resell_usd=resale), offer_usd=bid))
        offer_left -= bid
        taken.add(cand.nft)

    return plan
