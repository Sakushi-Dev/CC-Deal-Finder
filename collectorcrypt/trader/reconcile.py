"""Reconciliation foundation.

Reconciliation answers one question after any interruption: *"what is the real
state of the orders we believe are still in flight?"* It is the safety net that
keeps the persisted state honest across restarts and against the real
CollectorCrypt / Solana state.

Two layers, increasing authority
--------------------------------
* :class:`Reconciler` (since ETAPPE 2) — a **read-only** inspector. It loads the
  orders the store still considers active and *reports* stale/inconsistent ones.
  It never mutates an order: guessing an order's fate without evidence is
  exactly the silent automatism the project forbids.
* :class:`StatusSyncer` (ETAPPE 6) — the **authoritative** sync. Given the
  authenticated trading client it asks CollectorCrypt for the real status of
  each in-flight order and transitions it accordingly (``PENDING`` buy ->
  ``CONFIRMED``, accepted offer -> ``CONFIRMED``, cancelled listing/offer ->
  ``CANCELLED``). It only acts on **evidence** from the API; anything it cannot
  resolve is left untouched and reported. A confirmed buy additionally spawns
  its linked relist candidate so the exit flow can pick it up.

Both layers are side-effect-safe by default: on any error or ambiguity they
leave the order in its current state rather than risk a wrong transition.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .executor import _is_cancelled, _is_confirmed, _is_filled
from .orders import Order, OrderKind, OrderStatus, relist_order_for
from .store import OrderStore

logger = logging.getLogger("collectorcrypt.trader.reconcile")

# How long an order may sit in an active state before it is flagged as stale.
STALE_AFTER_SEC = float(os.environ.get("TRADER_RECONCILE_STALE_SEC", "900"))


@dataclass
class ReconciliationReport:
    """A read-only snapshot of the trader's outstanding obligations."""

    ts: float = field(default_factory=time.time)
    active: int = 0
    open_offers: int = 0
    relist_candidates: int = 0
    stale: list[dict[str, Any]] = field(default_factory=list)
    inconsistencies: list[dict[str, Any]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.stale and not self.inconsistencies

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "active": self.active,
            "open_offers": self.open_offers,
            "relist_candidates": self.relist_candidates,
            "stale": self.stale,
            "inconsistencies": self.inconsistencies,
            "healthy": self.healthy,
        }


class Reconciler:
    """Inspects persisted state for outstanding and inconsistent orders."""

    def __init__(self, store: OrderStore, *,
                 stale_after_sec: float = STALE_AFTER_SEC) -> None:
        self._store = store
        self._stale_after = stale_after_sec

    def reconcile(self) -> ReconciliationReport:
        """Build a reconciliation report. Pure read — never mutates orders."""
        now = time.time()
        active = self._store.active_orders()
        open_offers = self._store.open_offers()
        relist = self._store.relist_candidates()

        report = ReconciliationReport(
            ts=now,
            active=len(active),
            open_offers=len(open_offers),
            relist_candidates=len(relist),
        )

        for order in active:
            issue = self._inconsistency(order)
            if issue:
                report.inconsistencies.append(issue)
            if (now - order.updated_at) > self._stale_after:
                report.stale.append({
                    "id": order.id,
                    "kind": order.kind.value,
                    "status": order.status.value,
                    "nft": order.nft,
                    "name": order.name,
                    "age_sec": round(now - order.updated_at, 1),
                })
        return report

    @staticmethod
    def _inconsistency(order: Order) -> dict[str, Any] | None:
        """Return a structured issue if the order is in an impossible state.

        A *simulated* (dry-run/demo) order should always resolve to a terminal
        or resting state within the same cycle. Finding one still "active" in
        the store means a dry-run was interrupted — harmless on-chain, but a
        signal that the persisted state needs a cleanup.
        """
        if order.simulated and order.is_active:
            return {
                "id": order.id,
                "kind": order.kind.value,
                "status": order.status.value,
                "nft": order.nft,
                "reason": "simulated order left in an active state "
                          "(interrupted dry-run)",
            }
        return None


@dataclass
class StatusSyncReport:
    """Outcome of one authoritative status sync against CollectorCrypt."""

    ts: float = field(default_factory=time.time)
    checked: int = 0
    confirmed: int = 0
    cancelled: int = 0
    relisted_spawned: int = 0
    unresolved: int = 0
    errors: int = 0
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "checked": self.checked,
            "confirmed": self.confirmed,
            "cancelled": self.cancelled,
            "relisted_spawned": self.relisted_spawned,
            "unresolved": self.unresolved,
            "errors": self.errors,
            "transitions": self.transitions,
        }


class StatusSyncer:
    """Resolves in-flight orders against CollectorCrypt's authoritative status.

    This is the live reconciliation step (ETAPPE 6). For every active order it
    queries the API and, **only on clear evidence**, advances the order:

    * ``PENDING`` buy/listing reported confirmed -> ``CONFIRMED``;
    * ``OPEN`` offer reported accepted/filled -> ``CONFIRMED``;
    * any order reported cancelled/withdrawn/expired -> ``CANCELLED``.

    A confirmed buy with a positive resale price spawns its linked relist
    candidate (``PLANNED`` ``LIST`` order) so the exit flow lists it next pass.

    Failure-safe by construction: orders with no external id, an unreadable
    status, or an ambiguous response are left untouched and counted as
    *unresolved*. Read calls are the client's safe, retryable reads; a read
    error never transitions an order.

    The remote status shapes are reverse-engineered (see docs/api.md); the
    interpretation is deliberately defensive.
    """

    def __init__(self, store: OrderStore, *, client: Any,
                 wallet: str = "") -> None:
        self._store = store
        self._client = client
        self._wallet = wallet

    def sync(self) -> StatusSyncReport:
        report = StatusSyncReport()
        active = self._store.active_orders()
        if not active:
            return report

        for order in active:
            report.checked += 1
            try:
                remote = self._fetch_status(order)
            except Exception as exc:  # noqa: BLE001 - a read error must never transition an order
                report.errors += 1
                logger.warning("Status read failed for order %s: %s",
                               order.id, exc)
                continue

            if remote is None:
                report.unresolved += 1
                continue

            applied = self._apply(order, remote, report)
            if not applied:
                report.unresolved += 1
        return report

    # ------------------------------------------------------------------ #
    # Remote lookup
    # ------------------------------------------------------------------ #
    def _fetch_status(self, order: Order) -> dict[str, Any] | None:
        """Return the remote status payload for ``order``, or ``None``.

        VERIFIED (probe 2026-06-06): ``checkListingStatus`` is an RPC keyed by
        ``{nftAddress, wallet}`` (not a receipt/listing id). All order kinds use
        the same listing-status probe; without an nft address or wallet there is
        nothing to look up and the order stays unresolved.
        """
        if not order.nft or not self._wallet:
            return None
        resp = self._client.check_listing_status(
            nft=order.nft, wallet=self._wallet)
        return resp if isinstance(resp, dict) else None

    # ------------------------------------------------------------------ #
    # Transition application
    # ------------------------------------------------------------------ #
    def _apply(self, order: Order, remote: dict[str, Any],
               report: StatusSyncReport) -> bool:
        """Transition ``order`` from authoritative ``remote`` status. Evidence
        only — returns ``False`` when the response is ambiguous."""
        if _is_cancelled(remote):
            self._transition(order, OrderStatus.CANCELLED,
                             detail="remote status: cancelled", report=report)
            report.cancelled += 1
            return True

        confirmed = _is_confirmed(remote)
        if order.kind is OrderKind.OFFER:
            confirmed = confirmed or _is_filled(remote)
        if confirmed:
            self._transition(order, OrderStatus.CONFIRMED,
                             detail="remote status: confirmed", report=report)
            report.confirmed += 1
            if order.kind is OrderKind.BUY and order.resell_usd > 0:
                if self._spawn_relist(order):
                    report.relisted_spawned += 1
            return True
        return False

    def _transition(self, order: Order, status: OrderStatus, *, detail: str,
                    report: StatusSyncReport) -> None:
        prev = order.status.value
        try:
            order.transition(status, detail=detail)
            self._store.upsert_order(order)
        except Exception as exc:  # noqa: BLE001 - an illegal transition is reported, not fatal
            report.errors += 1
            logger.warning("Could not apply %s -> %s on order %s: %s",
                           prev, status.value, order.id, exc)
            return
        report.transitions.append({
            "id": order.id,
            "kind": order.kind.value,
            "nft": order.nft,
            "from": prev,
            "to": status.value,
        })

    def _spawn_relist(self, buy: Order) -> bool:
        """Create + persist the linked relist candidate for a confirmed buy.

        Idempotent: if the relist already exists (same client_order_id) it is
        not duplicated. This is what lets a buy confirmed *later* (by this sync,
        not at broadcast time) still flow into the exit/relisting step.
        """
        relist = relist_order_for(buy)
        try:
            existing = self._store.get_by_client_order_id(
                relist.client_order_id)
            if existing is not None:
                return False
            relist.detail = "relist candidate (spawned by status sync)"
            self._store.upsert_order(relist)
            return True
        except Exception as exc:  # noqa: BLE001 - relisting must not break the sync loop
            logger.warning("Could not spawn relist for buy %s: %s",
                           buy.id, exc)
            return False
