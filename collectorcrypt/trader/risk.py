"""Risk engine — the last gate before any live order is sent.

Where the strategy decides *what* would be profitable and the executor decides
*how* to send it safely, the risk engine decides *whether the bot is allowed to
act at all right now*. It enforces hard, operator-set limits and a kill switch,
independently of the planner, so a planning bug, a market anomaly or a runaway
loop cannot drain the wallet.

Design principles
-----------------
* **Fail-safe.** Any uncertainty — an unreadable store, a tripped kill switch —
  resolves to *do not trade*. The default outcome of an error is zero orders.
* **Pure decision, no side effects.** :meth:`RiskEngine.evaluate` reads the
  durable state and returns a :class:`RiskDecision`; it never mutates orders or
  sends anything. The engine applies the decision (failing blocked orders).
* **Opt-in limits.** Every limit defaults to ``0`` (disabled) so existing
  setups behave exactly as before until an operator sets a cap. A disabled
  limit is never a reason to block.
* **Observable.** The decision carries a full posture snapshot (limits, current
  usage, this cycle's planned spend, breaches) for the report and the UI.

Enforced controls
------------------
1. **Consecutive-failure kill switch** — after N real orders fail in a row, halt
   *all* trading this cycle. A burst of failures signals something is wrong
   (bad auth, API change, broken signing); stop rather than hammer on.
2. **Max open positions** — cap the number of real, in-flight orders so the bot
   cannot accumulate unbounded exposure faster than it can reconcile.
3. **Per-cycle spend cap** — a ceiling on the USD committed in a single cycle.
4. **Rolling daily spend cap** — a ceiling on realized USD spend over the last
   24 hours, across cycles.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .orders import Order, OrderKind

logger = logging.getLogger("collectorcrypt.trader.risk")

# Rolling window for the daily spend cap.
DAY_SECONDS = 24 * 60 * 60


@dataclass
class RiskDecision:
    """The outcome of evaluating a batch of planned orders against the limits."""

    allowed: list[Order] = field(default_factory=list)
    blocked: list[tuple[Order, str]] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    breaches: list[str] = field(default_factory=list)
    posture: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked_orders(self) -> list[Order]:
        return [order for order, _ in self.blocked]


class RiskEngine:
    """Evaluates planned orders against operator-set limits and a kill switch.

    Construct with the immutable config and (optionally) the durable store. With
    no store the current-usage queries return zero, so only the per-cycle spend
    cap can bind — appropriate for stateless previews.
    """

    # Only spending orders are subject to the caps; relists (sells) never spend.
    _SPEND_KINDS = (OrderKind.BUY, OrderKind.OFFER)

    def __init__(self, cfg, store=None) -> None:
        self._cfg = cfg
        self._store = store

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def posture(self) -> dict[str, Any]:
        """Current risk posture with no pending orders (read-only preview)."""
        return self.evaluate([]).posture

    def evaluate(self, orders: list[Order]) -> RiskDecision:
        """Decide which of ``orders`` may proceed under the current limits.

        Never raises: a failure to read usage is treated as a halt (fail-safe),
        so the caller can simply respect ``decision.allowed``.
        """
        limits = self._limits()
        try:
            usage = self._usage()
        except Exception as exc:  # noqa: BLE001 - unreadable state must not trade
            logger.warning("Risk usage read failed; halting trading: %s", exc)
            return RiskDecision(
                allowed=[], blocked=[(o, "risk state unreadable") for o in orders],
                halted=True, halt_reason=f"risk state unreadable: {exc}",
                breaches=["risk state unreadable"],
                posture=self._posture(limits, usage={}, cycle_spend=0.0,
                                      allowed=0, blocked=len(orders),
                                      halted=True,
                                      halt_reason="risk state unreadable",
                                      breaches=["risk state unreadable"]),
            )

        # 1) Kill switch: a run of failures halts everything this cycle.
        halt_reason = self._kill_switch_reason(limits, usage)
        if halt_reason:
            breaches = [halt_reason]
            return RiskDecision(
                allowed=[],
                blocked=[(o, halt_reason) for o in orders],
                halted=True, halt_reason=halt_reason, breaches=breaches,
                posture=self._posture(limits, usage, cycle_spend=0.0,
                                      allowed=0, blocked=len(orders),
                                      halted=True, halt_reason=halt_reason,
                                      breaches=breaches),
            )

        # 2) Per-order caps, evaluated in plan order so earlier (cheaper-first)
        #    orders win the remaining headroom.
        allowed: list[Order] = []
        blocked: list[tuple[Order, str]] = []
        breaches: list[str] = []
        cycle_spend = 0.0
        open_count = int(usage.get("open_positions", 0))
        day_spend = float(usage.get("spend_today", 0.0))

        for order in orders:
            cost = self._order_cost(order)
            reason = ""

            if (limits["max_open_positions"] > 0
                    and order.kind in self._SPEND_KINDS
                    and open_count + 1 > limits["max_open_positions"]):
                reason = (f"max open positions reached "
                          f"({limits['max_open_positions']})")
            elif (limits["max_spend_per_cycle_usd"] > 0
                    and cycle_spend + cost > limits["max_spend_per_cycle_usd"]):
                reason = (f"per-cycle spend cap reached "
                          f"(${limits['max_spend_per_cycle_usd']:.0f})")
            elif (limits["max_spend_per_day_usd"] > 0
                    and day_spend + cycle_spend + cost
                    > limits["max_spend_per_day_usd"]):
                reason = (f"daily spend cap reached "
                          f"(${limits['max_spend_per_day_usd']:.0f})")

            if reason:
                blocked.append((order, reason))
                if reason not in breaches:
                    breaches.append(reason)
            else:
                allowed.append(order)
                if order.kind in self._SPEND_KINDS:
                    cycle_spend += cost
                    open_count += 1

        return RiskDecision(
            allowed=allowed, blocked=blocked, halted=False, halt_reason="",
            breaches=breaches,
            posture=self._posture(limits, usage, cycle_spend=cycle_spend,
                                  allowed=len(allowed), blocked=len(blocked),
                                  halted=False, halt_reason="",
                                  breaches=breaches),
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _limits(self) -> dict[str, float]:
        return {
            "max_spend_per_cycle_usd": max(0.0, float(
                getattr(self._cfg, "max_spend_per_cycle_usd", 0.0))),
            "max_spend_per_day_usd": max(0.0, float(
                getattr(self._cfg, "max_spend_per_day_usd", 0.0))),
            "max_open_positions": max(0, int(
                getattr(self._cfg, "max_open_positions", 0))),
            "max_consecutive_failures": max(0, int(
                getattr(self._cfg, "max_consecutive_failures", 0))),
        }

    def _usage(self) -> dict[str, Any]:
        """Current usage from the durable store (zeros when no store)."""
        if self._store is None:
            return {"open_positions": 0, "spend_today": 0.0,
                    "consecutive_failures": 0}
        since = time.time() - DAY_SECONDS
        return {
            "open_positions": self._store.open_position_count(),
            "spend_today": self._store.confirmed_spend_since(since),
            "consecutive_failures": _leading_failures(
                self._store.recent_terminal_statuses()),
        }

    def _kill_switch_reason(self, limits: dict[str, float],
                            usage: dict[str, Any]) -> str:
        limit = limits["max_consecutive_failures"]
        if limit <= 0:
            return ""
        fails = int(usage.get("consecutive_failures", 0))
        if fails >= limit:
            return (f"kill switch: {fails} consecutive failures "
                    f">= limit {int(limit)}")
        return ""

    def _order_cost(self, order: Order) -> float:
        if order.kind in self._SPEND_KINDS:
            return max(0.0, float(order.price_usd))
        return 0.0

    def _posture(self, limits: dict[str, float], usage: dict[str, Any], *,
                 cycle_spend: float, allowed: int, blocked: int,
                 halted: bool, halt_reason: str,
                 breaches: list[str]) -> dict[str, Any]:
        enabled = any([
            limits["max_spend_per_cycle_usd"] > 0,
            limits["max_spend_per_day_usd"] > 0,
            limits["max_open_positions"] > 0,
            limits["max_consecutive_failures"] > 0,
        ])
        return {
            "enabled": enabled,
            "halted": halted,
            "halt_reason": halt_reason,
            "limits": limits,
            "usage": {
                "open_positions": int(usage.get("open_positions", 0)),
                "spend_today": round(float(usage.get("spend_today", 0.0)), 2),
                "consecutive_failures": int(
                    usage.get("consecutive_failures", 0)),
            },
            "cycle": {
                "planned_spend": round(cycle_spend, 2),
                "allowed": allowed,
                "blocked": blocked,
            },
            "breaches": list(breaches),
        }


def _leading_failures(statuses: list[str]) -> int:
    """Count consecutive ``failed`` statuses from the newest-first list.

    The first non-failed settled order (a confirmation or cancellation) breaks
    the streak — a success in between means the bot is not stuck failing.
    """
    count = 0
    for status in statuses:
        if status == "failed":
            count += 1
        else:
            break
    return count
