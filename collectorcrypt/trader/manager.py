"""Background orchestration for the trader UI.

A :class:`TraderManager` lives in the Flask app context (mirroring
``ScanManager``). It runs trade cycles in a worker thread, keeps a thread-safe
snapshot for the ``/trader/status`` endpoint, and appends each completed cycle
to a local, git-ignored history log so the UI can show profit/loss over time.

Everything stays dry-run unless the underlying engine is in live mode; this
manager never bypasses that gate.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import load_config
from .engine import TradeEngine
from .wallet import WalletError

HISTORY_PATH = Path(os.environ.get("TRADER_HISTORY_PATH", "trade_history.jsonl"))
_HISTORY_KEEP = 200


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
        self._load_history()

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
                return
            self._loop_active = True
            self._paused = False
            self._worker = threading.Thread(target=self._loop, daemon=True)
            self._worker.start()

    def pause(self) -> None:
        with self._lock:
            if self._loop_active:
                self._paused = True

    def resume(self) -> None:
        with self._lock:
            if self._loop_active:
                self._paused = False

    def stop(self) -> None:
        with self._lock:
            self._loop_active = False
            self._paused = False

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
            engine = TradeEngine(cfg)
            report = engine.run_cycle(sim_volume=sim_volume)
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
        if record_entry is not None:
            self._append_history(record_entry)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            history = list(self._history)
            return {
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
            }

    # ------------------------------------------------------------------ #
    # History persistence
    # ------------------------------------------------------------------ #
    def _load_history(self) -> None:
        try:
            lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-_HISTORY_KEEP:]:
            line = line.strip()
            if not line:
                continue
            try:
                self._history.append(json.loads(line))
            except ValueError:
                continue

    def _append_history(self, record: dict[str, Any]) -> None:
        try:
            with HISTORY_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _history_record(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": time.time(),
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
