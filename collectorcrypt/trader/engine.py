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

import uuid
from typing import Any

from ..api import CCClient
from ..normalize import normalize_card
from .auth import SessionProvider
from .ccapi import CCApiError, CCTradingClient
from .config import TraderConfig
from .executor import DryRunExecutor, Executor, LiveExecutor
from .orders import Order, OrderKind, OrderStatus, plan_to_orders
from .reconcile import StatusSyncer
from .risk import RiskEngine
from .siws import make_session_provider
from .store import OrderStore
from .strategy import (BuyPlan, build_plan, diagnose_listings,
                       make_candidates, make_offer_candidates)
from .wallet import Wallet


class TradeEngine:
    """Runs trade cycles for a single wallet/config."""

    def __init__(self, cfg: TraderConfig, *, client: CCClient | None = None,
                 wallet: Wallet | None = None,
                 store: OrderStore | None = None,
                 session_provider: SessionProvider | None = None) -> None:
        self._cfg = cfg
        self._client = client or CCClient()
        self._wallet = wallet or Wallet(
            cfg.rpc_url, address=cfg.wallet_address, secret=cfg.wallet_secret
        )
        self._store = store
        # The auth seam (ETAPPE 3/4). Defaults to the configured provider, which
        # is the safe NullSessionProvider unless the operator opted into a real
        # one via TRADER_AUTH_PROVIDER. Live execution (ETAPPE 5) uses this.
        self._session_provider = session_provider or make_session_provider(
            cfg, self._wallet
        )

    @property
    def executor(self) -> Executor:
        # Live execution requires ALL of: the master switch, a signing wallet,
        # and a configured (non-null) auth provider. Anything less falls back to
        # the dry-run executor so a half-configured live setup can never spend.
        # The budget-aware variant is built per cycle in run_cycle; this
        # property exposes the gating decision (and the live executor type) with
        # an empty budget for callers that only inspect the mode.
        return self._build_executor(0.0)

    def _is_live_armed(self) -> bool:
        """True only when every live precondition is satisfied."""
        return bool(
            self._cfg.live and self._wallet.can_sign
            and (self._cfg.auth_provider or "none").lower() != "none"
        )

    def _build_executor(self, available_volume: float, *,
                        demo: bool = False) -> Executor:
        """Construct the executor for a cycle, wiring live context when armed.

        The live executor is given everything it needs to run safely: the
        authenticated trading client, the durable store (for incremental state
        + duplicate prevention) and the cycle budget envelope. The dry-run
        executor needs none of this.

        Demo cycles are **always** dry-run: a hypothetical volume must never
        touch the live trading path, even on a fully armed wallet.
        """
        if demo:
            return DryRunExecutor()
        if self._is_live_armed():
            return LiveExecutor(
                self._wallet, self._cfg.rpc_url,
                session_provider=self._session_provider,
                client=CCTradingClient(
                    session_provider=self._session_provider),
                store=self._store,
                available_volume=available_volume,
                cfg=self._cfg,
            )
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
    def run_cycle(self, *, sim_volume: float | None = None,
                  persist: bool = True) -> dict[str, Any]:
        """Run one decision cycle.

        When ``sim_volume`` is given the wallet is **not** read at all; the bot
        plans against that hypothetical USDC volume instead. This powers the
        "demo mode" in the UI, letting the user see how the bot would react to
        any budget without owning the funds (and without a configured wallet).

        When a store is attached and ``persist`` is true, the cycle header and
        all resulting orders are written durably. Demo cycles are never
        persisted (they did not happen on-chain and must not pollute the real
        order ledger).
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

        executor = self._build_executor(available_volume, demo=demo)
        live = isinstance(executor, LiveExecutor) and not demo
        cycle_id = uuid.uuid4().hex
        # The plan is materialised into typed PLANNED orders; the executor then
        # transitions them (dry-run resolves in-memory, live would send/sign/
        # broadcast). Both modes share this one pipeline.
        planned_orders = plan_to_orders(plan, cycle_id, simulated=not live)

        # Risk gate (ETAPPE 7): the final guard before any live order is sent.
        # On live cycles, operator-set limits and the kill switch decide which
        # planned orders may proceed; blocked orders are failed safely (never
        # sent) and never reach the executor. Dry-run/demo are unaffected but
        # the posture is still computed for display.
        risk_decision = None
        risk_blocked: list[Order] = []
        if live:
            risk_decision = RiskEngine(self._cfg, self._store).evaluate(
                planned_orders)
            for order, reason in risk_decision.blocked:
                if not order.is_terminal:
                    order.transition(OrderStatus.FAILED,
                                    detail=f"risk gate: {reason}",
                                    error=f"blocked by risk limit: {reason}")
            risk_blocked = risk_decision.blocked_orders
            executable = risk_decision.allowed
        else:
            executable = planned_orders

        executed = executor.execute(executable) if executable else []
        # The report sees every planned order: those executed plus those the
        # risk gate failed (so blocked intents are visible, not silently gone).
        orders = executed + risk_blocked

        report = _report(self._cfg, self._wallet, sol_rate, sol_balance,
                         usdc_balance, available_volume, listings, candidates,
                         plan, orders, live, demo=demo, near_misses=near_misses,
                         cycle_id=cycle_id)
        if risk_decision is not None:
            report["risk"] = risk_decision.posture

        # Live maintenance: after placing new buys/offers, (1) reconcile any
        # in-flight orders against CollectorCrypt's authoritative status, then
        # (2) run the exit flow to list cards from confirmed buys. The order
        # matters — a buy confirmed by the sync spawns a relist candidate that
        # the exit pass can then list in the same cycle. Both are no-ops in
        # dry-run/demo. The exit pass is skipped while the risk kill switch is
        # tripped (something is wrong; do not sign/send more), but the
        # read-only status sync still runs to resolve in-flight state.
        if live and self._store is not None:
            report["status_sync"] = self._run_status_sync()
            halted = bool(risk_decision and risk_decision.halted)
            if halted:
                report["relisted"] = []
            else:
                report["relisted"] = self._run_exit_flow(executor)

        # Durable persistence: real cycles only. Demo never touches the ledger.
        if self._store is not None and persist and not demo:
            self._persist_cycle(cycle_id, report, orders)
        return report

    # ------------------------------------------------------------------ #
    # Live maintenance (ETAPPE 6)
    # ------------------------------------------------------------------ #
    def _run_status_sync(self) -> dict[str, Any]:
        """Reconcile in-flight orders against CC's authoritative status.

        Never raises: a maintenance failure must not abort a trading cycle. On
        error the report carries an ``error`` field for the operator.
        """
        try:
            syncer = StatusSyncer(
                self._store,  # type: ignore[arg-type]
                client=CCTradingClient(session_provider=self._session_provider),
                wallet=self._wallet.address,
            )
            return syncer.sync().to_dict()
        except CCApiError as exc:
            return {"error": f"status sync failed: {exc}"}
        except Exception as exc:  # noqa: BLE001 - maintenance must never crash a cycle
            return {"error": f"status sync error: {exc}"}

    def _run_exit_flow(self, executor: Executor) -> list[dict[str, Any]]:
        """List cards from confirmed buys (the live exit/relisting flow).

        Loads the persisted ``PLANNED`` relist candidates and drives each onto
        the market via the live executor. Returns a compact per-listing summary
        for the report/UI. Never raises.
        """
        if not isinstance(executor, LiveExecutor):
            return []
        try:
            candidates = self._store.relist_candidates()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"could not load relist candidates: {exc}"}]
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            listed = executor.relist(candidate)
            results.append({
                "name": listed.name,
                "nft": listed.nft,
                "status": listed.status.value,
                "price_usd": round(listed.price_usd, 2),
                "market_usd": round(listed.market_usd, 2),
                "ok": listed.succeeded,
                "detail": listed.detail,
                "category": listed.category,
            })
        return results

    def _persist_cycle(self, cycle_id: str, report: dict[str, Any],
                       orders: list[Order]) -> None:
        """Write the cycle header (with a redacted config snapshot) and orders."""
        try:
            self._store.save_cycle(  # type: ignore[union-attr]
                cycle_id,
                mode=report["mode"],
                wallet=report["wallet"],
                demo=report["demo"],
                config_snapshot=_config_snapshot(self._cfg),
                summary=_cycle_summary(report),
            )
            self._store.save_orders(orders)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - persistence must not crash a cycle
            report["persist_error"] = f"Failed to persist cycle: {exc}"


def _order_states(orders: list[Order]) -> dict[str, int]:
    """Count orders by status — a compact, observable cycle summary."""
    counts: dict[str, int] = {}
    for o in orders:
        counts[o.status.value] = counts.get(o.status.value, 0) + 1
    return counts


def _config_snapshot(cfg: TraderConfig) -> dict[str, Any]:
    """A redacted snapshot of the config that drove a cycle.

    The wallet **secret is never included** — only its presence is recorded as
    a boolean so an operator can later see whether the cycle ran with signing
    capability, without the key ever touching the database.
    """
    return {
        "rpc_url": cfg.rpc_url,
        "wallet_address": cfg.wallet_address,
        "has_secret": cfg.has_secret,
        "live": cfg.live,
        "reserve_usdc": cfg.reserve_usdc,
        "gas_reserve_sol": cfg.gas_reserve_sol,
        "base_max_card_usd": cfg.base_max_card_usd,
        "min_card_usd": cfg.min_card_usd,
        "min_discount_pct": cfg.min_discount_pct,
        "direct_buy_pct": cfg.direct_buy_pct,
        "offer_pct": cfg.offer_pct,
        "offer_discount_pct": cfg.offer_discount_pct,
        "offer_max_premium_pct": cfg.offer_max_premium_pct,
        "resell_discount_pct": cfg.resell_discount_pct,
        "escalation_volume_usd": cfg.escalation_volume_usd,
        "escalation_max_card_usd": cfg.escalation_max_card_usd,
        "max_spend_per_cycle_usd": cfg.max_spend_per_cycle_usd,
        "max_spend_per_day_usd": cfg.max_spend_per_day_usd,
        "max_open_positions": cfg.max_open_positions,
        "max_consecutive_failures": cfg.max_consecutive_failures,
        "offer_bump_usd": cfg.offer_bump_usd,
        "offer_bump_age_hours": cfg.offer_bump_age_hours,
        "offer_bump_max": cfg.offer_bump_max,
        "min_operate_usd": cfg.min_operate_usd,
        "max_owned_cards": cfg.max_owned_cards,
        "unpopular_days": cfg.unpopular_days,
        "markdown_delay_days": cfg.markdown_delay_days,
        "markdown_step_pct": cfg.markdown_step_pct,
        "markdown_interval_days": cfg.markdown_interval_days,
        "offer_accept_delay_days": cfg.offer_accept_delay_days,
        "offer_accept_min_market_pct": cfg.offer_accept_min_market_pct,
        "market_recheck_hours": cfg.market_recheck_hours,
        "categories": list(cfg.categories),
        "max_pages": cfg.max_pages,
        "allowed_marketplaces": list(cfg.allowed_marketplaces),
    }


# Report keys persisted as the per-cycle summary (drives history + totals).
_SUMMARY_KEYS = (
    "mode", "available_volume", "scanned", "candidates", "planned_buys",
    "planned_cost", "planned_profit", "planned_resell_profit",
    "planned_offers", "planned_offer_cost", "planned_offer_profit",
    "fills_ok", "order_states",
)


def _cycle_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {key: report.get(key) for key in _SUMMARY_KEYS}


def _report(cfg: TraderConfig, wallet: Wallet, sol_rate: float,
            sol_balance: float, usdc_balance: float, available_volume: float,
            listings: list, candidates: list, plan: BuyPlan,
            orders: list[Order], live: bool, demo: bool = False,
            near_misses: list | None = None,
            cycle_id: str = "") -> dict[str, Any]:
    # "Book" profit if everything sold at full insured value (upper bound).
    planned_profit = sum(c.market_usd - c.ask_usd for c in plan.items)
    offer_profit = sum(o.candidate.market_usd - o.offer_usd for o in plan.offers)
    # Realistic profit using the resale rule: relist below market value.
    resell_profit = plan.resell_profit
    offer_resell_profit = plan.offer_resell_profit
    mode = "DEMO" if demo else ("LIVE" if live else "DRY-RUN")
    # Orders that represent an external trading action (buys + offers); LIST
    # orders are the relist side and are reported separately/implicitly.
    action_orders = [o for o in orders
                     if o.kind in (OrderKind.BUY, OrderKind.OFFER)]
    return {
        "mode": mode,
        "demo": demo,
        "cycle_id": cycle_id,
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
        "fills_ok": sum(1 for o in action_orders if o.succeeded),
        "order_states": _order_states(orders),
        "near_misses": near_misses or [],
        "executed": [
            {
                "name": o.name,
                "nft": o.nft,
                "kind": o.kind.value,  # "buy" or "offer"
                "status": o.status.value,
                "price_usd": round(o.price_usd, 2),
                "market_usd": round(o.market_usd, 2),
                "resell_usd": round(o.resell_usd, 2),
                "ok": o.succeeded,
                "simulated": o.simulated,
                "detail": o.detail,
                "category": o.category,
            }
            for o in action_orders
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
