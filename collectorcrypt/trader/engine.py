"""Trader orchestration.

Ties the pieces together for one decision cycle:

1. read SOL/USDC balance -> available volume (what the bot may still spend),
2. source CC listings via the existing :class:`CCClient` + normalizer,
3. build a quantity-first, CC-only :class:`BuyPlan` (with escalation),
4. execute it (dry-run by default; live only if explicitly enabled *and*
   the live executor is implemented).

Returns a plain ``dict`` report so it can be printed, logged or served as JSON.
"""
from __future__ import annotations

from typing import Any

from ..api import CCClient
from ..normalize import normalize_card
from .config import TraderConfig
from .executor import DryRunExecutor, Executor, Fill, LiveExecutor
from .strategy import (BuyPlan, build_plan, diagnose_listings,
                       make_candidates, make_offer_candidates)
from .wallet import Wallet


class TradeEngine:
    """Runs trade cycles for a single wallet/config."""

    def __init__(self, cfg: TraderConfig, *, client: CCClient | None = None,
                 wallet: Wallet | None = None) -> None:
        self._cfg = cfg
        self._client = client or CCClient()
        self._wallet = wallet or Wallet(
            cfg.rpc_url, address=cfg.wallet_address, secret=cfg.wallet_secret
        )

    @property
    def executor(self) -> Executor:
        if self._cfg.live and self._wallet.can_sign:
            return LiveExecutor(self._wallet, self._cfg.rpc_url)
        return DryRunExecutor()

    # ------------------------------------------------------------------ #
    # Sourcing
    # ------------------------------------------------------------------ #
    def _collect_listings(self) -> list[dict[str, Any]]:
        """Fetch + normalize listings across the configured categories/pages."""
        from .. import config as app_config

        seen: set[str] = set()
        cards: list[dict[str, Any]] = []
        for category in self._cfg.categories:
            want = category.strip().lower()
            for page in range(1, self._cfg.max_pages + 1):
                data = self._client.fetch_marketplace_page_with_retry(
                    page, app_config.SCAN_STEP
                )
                raw = data.get("filterNFtCard") or []
                if not raw:
                    break
                for c in raw:
                    n = normalize_card(c)
                    nft = n.get("nft")
                    if not nft or nft in seen:
                        continue
                    # The marketplace API ignores the category param, so filter
                    # client-side (mirrors ScanManager). Empty = all categories.
                    if want and (n.get("category") or "").lower() != want:
                        continue
                    seen.add(nft)
                    cards.append(n)
                if page >= int(data.get("totalPages") or 1):
                    break
        return cards

    # ------------------------------------------------------------------ #
    # Cycle
    # ------------------------------------------------------------------ #
    def run_cycle(self, *, sim_volume: float | None = None) -> dict[str, Any]:
        """Run one decision cycle.

        When ``sim_volume`` is given the wallet is **not** read at all; the bot
        plans against that hypothetical USDC volume instead. This powers the
        "demo mode" in the UI, letting the user see how the bot would react to
        any budget without owning the funds (and without a configured wallet).
        """
        sol_rate = self._client.fetch_sol_usd()
        demo = sim_volume is not None
        if demo:
            sol_balance = 0.0
            usdc_balance = max(0.0, float(sim_volume))
            available_volume = max(0.0, float(sim_volume))
        else:
            sol_balance = self._wallet.sol_balance()
            usdc_balance = self._wallet.usdc_balance()
            available_volume = max(
                0.0, usdc_balance - max(0.0, self._cfg.reserve_usdc)
            )

        listings = self._collect_listings()
        candidates = make_candidates(listings, sol_rate, self._cfg)
        offer_candidates = make_offer_candidates(listings, sol_rate, self._cfg)
        plan = build_plan(candidates, available_volume, self._cfg,
                          offer_candidates=offer_candidates)
        # Always surface the closest deals (qualifying or not) so the UI / demo
        # can show which hypothetical cards are near the buy threshold.
        near_misses = diagnose_listings(listings, sol_rate, self._cfg,
                                        plan.card_cap_usd)

        executor = self.executor
        live = isinstance(executor, LiveExecutor) and not demo
        fills = executor.execute(plan) if (plan.items or plan.offers) else []

        return _report(self._cfg, self._wallet, sol_rate, sol_balance,
                       usdc_balance, available_volume, listings, candidates,
                       plan, fills, live, demo=demo, near_misses=near_misses)


def _report(cfg: TraderConfig, wallet: Wallet, sol_rate: float,
            sol_balance: float, usdc_balance: float, available_volume: float,
            listings: list, candidates: list, plan: BuyPlan,
            fills: list[Fill], live: bool, demo: bool = False,
            near_misses: list | None = None) -> dict[str, Any]:
    # "Book" profit if everything sold at full insured value (upper bound).
    planned_profit = sum(c.market_usd - c.ask_usd for c in plan.items)
    offer_profit = sum(o.candidate.market_usd - o.offer_usd for o in plan.offers)
    # Realistic profit using the resale rule: relist below market value.
    resell_profit = plan.resell_profit
    offer_resell_profit = plan.offer_resell_profit
    mode = "DEMO" if demo else ("LIVE" if live else "DRY-RUN")
    return {
        "mode": mode,
        "demo": demo,
        "wallet": wallet.address,
        "sol_rate": sol_rate,
        "sol_balance": sol_balance,
        "usdc_balance": usdc_balance,
        "available_volume": available_volume,
        "scanned": len(listings),
        "candidates": len(candidates),
        "escalated": plan.escalated,
        "card_cap_usd": plan.card_cap_usd,
        "direct_budget": plan.direct_budget,
        "offer_budget": plan.offer_budget,
        "resell_discount_pct": cfg.resell_discount_pct,
        "planned_buys": plan.count,
        "planned_cost": plan.total_cost,
        "planned_profit": planned_profit,
        "planned_resell_value": plan.resell_value,
        "planned_resell_profit": resell_profit,
        "planned_offers": plan.offer_count,
        "planned_offer_cost": plan.offer_cost,
        "planned_offer_profit": offer_profit,
        "planned_offer_resell_profit": offer_resell_profit,
        "remaining_volume": plan.remaining_volume,
        "skipped": plan.skipped,
        "fills_ok": sum(1 for f in fills if f.ok),
        "near_misses": near_misses or [],
        "executed": [
            {
                "name": f.candidate.name,
                "nft": f.candidate.nft,
                "kind": f.kind,  # "buy" or "offer"
                "price_usd": round(f.price_usd, 2),
                "market_usd": round(f.candidate.market_usd, 2),
                "resell_usd": round(f.candidate.resell_usd, 2),
                "ok": f.ok,
                "simulated": f.simulated,
                "detail": f.detail,
                "category": f.candidate.card.get("category", ""),
            }
            for f in fills if f.kind in ("buy", "offer")
        ],
        "items": [
            {
                "name": c.name,
                "nft": c.nft,
                "ask_usd": round(c.ask_usd, 2),
                "market_usd": round(c.market_usd, 2),
                "resell_usd": round(c.resell_usd, 2),
                "resell_profit": round(c.resell_usd - c.ask_usd, 2),
                "discount_pct": round(c.discount_pct, 1),
                "category": c.card.get("category", ""),
                "currency": c.card.get("currency", ""),
            }
            for c in plan.items
        ],
        "offers": [
            {
                "name": o.name,
                "nft": o.nft,
                "ask_usd": round(o.candidate.ask_usd, 2),
                "offer_usd": round(o.offer_usd, 2),
                "market_usd": round(o.candidate.market_usd, 2),
                "resell_usd": round(o.candidate.resell_usd, 2),
                "resell_profit": round(o.candidate.resell_usd - o.offer_usd, 2),
                "category": o.candidate.card.get("category", ""),
                "currency": o.candidate.card.get("currency", ""),
            }
            for o in plan.offers
        ],
    }
