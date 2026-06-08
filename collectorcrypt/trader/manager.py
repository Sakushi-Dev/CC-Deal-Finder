"""Background orchestration for the trader UI.

A :class:`TraderManager` lives in the Flask app context (mirroring
``ScanManager``). It runs trade cycles in a worker thread, keeps a thread-safe
snapshot for the ``/trader/status`` endpoint, and appends each completed cycle
to a local, git-ignored history log so the UI can show profit/loss over time.

Everything stays dry-run unless the underlying engine is in live mode; this
manager never bypasses that gate.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from .config import load_config
from .engine import TradeEngine
from .reconcile import Reconciler
from .risk import RiskEngine
from .store import OrderStore
from .wallet import WalletError

_HISTORY_KEEP = 200

# Runtime store key holding the loop control state so it survives a restart.
_LOOP_STATE_KEY = "loop_state"


class TraderManager:
    """Runs trade cycles (single-shot or looped) and tracks state + history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False        # a cycle is executing right now
        self._loop_active = False    # auto-repeat is on
        self._paused = False         # loop is on but skipping execution
        self._interval = 300.0
        self._error: str | None = None
        self._last_report: dict[str, Any] | None = None
        self._updated_at: float | None = None
        self._cycles = 0
        self._worker: threading.Thread | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_KEEP)
        # Durable persistence + reconciliation foundation (ETAPPE 2).
        self._store = OrderStore()
        self._reconciler = Reconciler(self._store)
        # Crash-recovery summary for the operator (ETAPPE 8).
        self._recovery: dict[str, Any] = {"performed": False}
        self._load_history()
        self._recover()

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #
    def run_once(self) -> bool:
        """Run a single cycle now. Returns ``False`` if one is in progress."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._run_single, daemon=True).start()
        return True

    def run_demo(self, volume: float) -> bool:
        """Run a single hypothetical cycle against a simulated USDC volume.

        The wallet is not touched and the result is shown as the latest report
        but **not** written to history/totals (it never happened on-chain).
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(
            target=lambda: self._execute_cycle(sim_volume=max(0.0, float(volume)),
                                               record=False),
            daemon=True,
        ).start()
        return True

    def start_loop(self, interval: float) -> None:
        with self._lock:
            self._interval = max(15.0, float(interval or 300.0))
            if self._loop_active:
                self._paused = False
                self._persist_loop_state()
                return
            self._loop_active = True
            self._paused = False
            self._persist_loop_state()
            self._worker = threading.Thread(target=self._loop, daemon=True)
            self._worker.start()

    def pause(self) -> None:
        with self._lock:
            if self._loop_active:
                self._paused = True
                self._persist_loop_state()

    def resume(self) -> None:
        with self._lock:
            if self._loop_active:
                self._paused = False
                self._persist_loop_state()

    def stop(self) -> None:
        with self._lock:
            self._loop_active = False
            self._paused = False
            self._persist_loop_state()

    # ------------------------------------------------------------------ #
    # Persistence / recovery (ETAPPE 8)
    # ------------------------------------------------------------------ #
    def _persist_loop_state(self) -> None:
        """Write the current loop control state to the durable store.

        Called while holding ``self._lock``. A persistence failure must never
        break the live control flow, so errors are swallowed (the in-memory
        state stays authoritative for the running process).
        """
        try:
            self._store.set_runtime(_LOOP_STATE_KEY, {
                "loop_active": self._loop_active,
                "paused": self._paused,
                "interval": self._interval,
            })
        except Exception:  # noqa: BLE001 - persistence is best-effort
            pass

    def _recover(self) -> None:
        """Restore loop state after a restart and reconcile in-flight orders.

        Two independent, fail-safe steps:

        * **Startup reconcile** — a single read-only reconciliation so the UI
          immediately reflects any orders that were in flight when the process
          stopped. This never submits, signs or cancels anything.
        * **Opt-in auto-resume** — only when ``TRADER_AUTO_RESUME`` is set *and*
          the loop was active before the restart is the worker restarted, in
          exactly the mode that is configured now. The live/auth gates are
          unchanged, so a crash can never silently arm live trading.
        """
        summary: dict[str, Any] = {"performed": True, "auto_resume": False,
                                   "resumed": False, "in_flight": 0,
                                   "was_active": False}
        # 1) Read-only reconcile of any orders left in flight.
        try:
            recon = self._reconciler.reconcile().to_dict()
            summary["in_flight"] = int(recon.get("active", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - never block startup
            summary["reconcile_error"] = str(exc)

        # 2) Read persisted loop state.
        try:
            saved = self._store.get_runtime(_LOOP_STATE_KEY) or {}
        except Exception:  # noqa: BLE001
            saved = {}
        was_active = bool(saved.get("loop_active"))
        summary["was_active"] = was_active

        # 3) Opt-in resume (env-only flag, like the live switch).
        try:
            auto_resume = load_config().auto_resume
        except Exception:  # noqa: BLE001 - default to the safe choice
            auto_resume = False
        summary["auto_resume"] = bool(auto_resume)

        if auto_resume and was_active:
            interval = float(saved.get("interval") or 300.0)
            paused = bool(saved.get("paused"))
            with self._lock:
                self._interval = max(15.0, interval)
                self._loop_active = True
                self._paused = paused
                self._worker = threading.Thread(target=self._loop, daemon=True)
                self._worker.start()
            summary["resumed"] = True

        self._recovery = summary

    # ------------------------------------------------------------------ #
    # Workers
    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._loop_active:
                    return
                paused = self._paused
                interval = self._interval
            if not paused:
                self._execute_cycle()
            # Sleep in small slices so stop/resume react quickly.
            waited = 0.0
            while waited < interval:
                with self._lock:
                    if not self._loop_active:
                        return
                time.sleep(0.5)
                waited += 0.5

    def _run_single(self) -> None:
        self._execute_cycle()

    def _execute_cycle(self, sim_volume: float | None = None,
                       record: bool = True) -> None:
        with self._lock:
            self._running = True
            self._error = None
        try:
            cfg = load_config()
            engine = TradeEngine(cfg, store=self._store)
            report = engine.run_cycle(sim_volume=sim_volume, persist=record)
        except WalletError as exc:
            with self._lock:
                self._error = str(exc)
                self._running = False
                self._updated_at = time.time()
            return
        except Exception as exc:  # noqa: BLE001 - surface to UI
            with self._lock:
                self._error = f"Cycle error: {exc}"
                self._running = False
                self._updated_at = time.time()
            return

        record_entry = _history_record(report) if record else None
        with self._lock:
            self._last_report = report
            self._updated_at = time.time()
            self._running = False
            if record_entry is not None:
                self._cycles += 1
                self._history.append(record_entry)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            history = list(self._history)
            base = {
                "running": self._running,
                "loop_active": self._loop_active,
                "paused": self._paused,
                "interval": self._interval,
                "error": self._error,
                "cycles": self._cycles,
                "updated_at": self._updated_at,
                "report": self._last_report,
                "history": history[-50:],
                "totals": _aggregate(history),
                "recovery": dict(self._recovery),
            }
        # Reconciliation reads the store (outside the state lock to avoid
        # holding it during DB I/O). Failure here must never break the UI.
        try:
            base["reconciliation"] = self._reconciler.reconcile().to_dict()
            base["order_counts"] = self._store.counts_by_status()
        except Exception as exc:  # noqa: BLE001 - surface, never crash snapshot
            base["reconciliation"] = {"error": str(exc)}
            base["order_counts"] = {}
        # Holdings inventory + unpopular blacklist (ETAPPE 7). Read outside the
        # state lock and fail-safe to empty lists so a DB hiccup never breaks
        # the dashboard poll.
        try:
            base["holdings"] = [h.to_dict() for h in self._store.holdings_list()]
            base["blacklist"] = self._store.blacklisted_nfts()
        except Exception as exc:  # noqa: BLE001 - surface, never crash snapshot
            base["holdings"] = []
            base["blacklist"] = []
            base["holdings_error"] = str(exc)
        base["auth"] = self._auth_status()
        base["risk"] = self._risk_status()
        return base

    def clear_blacklist_entry(self, nft: str) -> None:
        """Remove an NFT from the unpopular blacklist (UI clear button).

        Thin delegate to the store; the route layer validates the address. A
        no-op on an unknown NFT (the store's ``clear_blacklist`` is idempotent).
        """
        self._store.clear_blacklist(nft)

    def _auth_status(self) -> dict[str, Any]:
        """Non-network summary of auth/live readiness for the operator panel.

        This never performs the SIWS handshake (no network from a UI poll); it
        only reports the configured posture so the dashboard can show whether
        live trading is armed and why it may be blocked.
        """
        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001
            return {"provider": "unknown", "live": False, "error": str(exc)}
        provider = (cfg.auth_provider or "none").lower()
        reasons: list[str] = []
        if not cfg.live:
            reasons.append("TRADER_LIVE is off")
        if not cfg.has_secret:
            reasons.append("no signing wallet (TRADER_WALLET_SECRET)")
        if provider == "none":
            reasons.append("no auth provider (TRADER_AUTH_PROVIDER=none)")
        return {
            "provider": provider,
            "live": cfg.live,
            "can_sign": cfg.has_secret,
            # "armed" = configuration permits live; it does NOT assert a live
            # session exists (that requires the network handshake at run time).
            "armed": not reasons,
            "blocked_reasons": reasons,
        }

    def _risk_status(self) -> dict[str, Any]:
        """Read-only risk posture (limits + current usage) for the operator.

        Computed from the durable store with no pending orders, so the panel
        shows the configured caps, today's spend, open positions and whether
        the kill switch would currently halt trading — even before a cycle
        runs. Never raises; the engine itself enforces these limits live.
        """
        try:
            cfg = load_config()
            return RiskEngine(cfg, self._store).posture()
        except Exception as exc:  # noqa: BLE001 - surface, never crash snapshot
            return {"error": str(exc)}

    # ------------------------------------------------------------------ #
    # History persistence (durable store)
    # ------------------------------------------------------------------ #
    def _load_history(self) -> None:
        """Rebuild the in-memory history cache from the durable store.

        This is what lets the dashboard show prior cycles immediately after an
        application restart instead of starting from an empty slate.
        """
        try:
            cycles = self._store.recent_cycles(_HISTORY_KEEP)
        except Exception:  # noqa: BLE001 - a fresh/locked DB must not crash boot
            return
        for entry in cycles:
            self._history.append(_history_record(entry))
        # The history deque is capped at _HISTORY_KEEP, so its length undercounts
        # the true cycle total after a restart. Read the authoritative count from
        # the store (falling back to the deque length if the query fails).
        try:
            self._cycles = self._store.cycle_count()
        except Exception:  # noqa: BLE001 - count is cosmetic, never crash boot
            self._cycles = len(self._history)


def _history_record(report: dict[str, Any]) -> dict[str, Any]:
    # Preserve an existing timestamp (when rebuilding from the store) so the
    # restored history keeps the real cycle times instead of "now".
    return {
        "ts": report.get("ts") or time.time(),
        "mode": report.get("mode"),
        "available_volume": report.get("available_volume"),
        "scanned": report.get("scanned"),
        "candidates": report.get("candidates"),
        "planned_buys": report.get("planned_buys"),
        "planned_cost": report.get("planned_cost"),
        "planned_profit": report.get("planned_profit"),
        "planned_resell_profit": report.get("planned_resell_profit"),
        "planned_offers": report.get("planned_offers"),
        "planned_offer_cost": report.get("planned_offer_cost"),
        "planned_offer_profit": report.get("planned_offer_profit"),
    }


def _aggregate(history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cycles": len(history),
        "buys": sum(int(h.get("planned_buys") or 0) for h in history),
        "cost": sum(float(h.get("planned_cost") or 0) for h in history),
        "profit": sum(float(h.get("planned_profit") or 0) for h in history),
        "resell_profit": sum(float(h.get("planned_resell_profit") or 0) for h in history),
        "offers": sum(int(h.get("planned_offers") or 0) for h in history),
    }
