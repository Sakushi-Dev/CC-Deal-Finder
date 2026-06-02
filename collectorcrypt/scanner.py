"""Hintergrund-Scanner für die Deals-Suche.

Eine :class:`ScanManager`-Instanz lebt im App-Kontext und kapselt:

* Lebenszyklus des Worker-Threads (start/pause/resume/stop).
* Threadsicheren Zugriff auf den Live-Status (für `/deals/status`).
* Die eigentliche Match-Logik (Preisspanne, Sticker-Ausschluss,
  Insured-Value-Vergleich).
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Iterable

import requests

from . import config
from .api import CCClient
from .normalize import normalize_card, to_usd


_INITIAL_STATE: dict[str, Any] = {
    "running": False,
    "paused": False,
    "stop_requested": False,
    "done": False,
    "error": None,
    "last_page_error": None,
    "failed_pages": [],
    "min_usd": None,
    "max_usd": None,
    "order": "shuffle",
    "sol_rate": None,
    "scanned": 0,
    "in_range": 0,
    "matched": 0,
    "page": 0,
    "total_pages": 0,
    "started_at": None,
    "updated_at": None,
    "deals": [],
}


class ScanManager:
    """Verwaltet einen einzelnen, optional pausierbaren Scan-Worker."""

    def __init__(self, client: CCClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._state: dict[str, Any] = dict(_INITIAL_STATE)
        self._state["deals"] = []
        self._state["failed_pages"] = []

    # ------------------------------------------------------------------ #
    # Steuerung
    # ------------------------------------------------------------------ #
    def start(self, min_usd: float, max_usd: float, order: str = "shuffle") -> bool:
        with self._lock:
            if self._state["running"]:
                return False
            self._state.update({
                "running": True, "paused": False, "stop_requested": False,
                "done": False, "error": None,
                "last_page_error": None, "failed_pages": [],
                "min_usd": min_usd, "max_usd": max_usd,
                "order": order, "sol_rate": None,
                "scanned": 0, "in_range": 0, "matched": 0,
                "page": 0, "total_pages": 0,
                "started_at": time.time(), "updated_at": time.time(),
                "deals": [],
            })
        t = threading.Thread(target=self._worker,
                             args=(min_usd, max_usd, order), daemon=True)
        t.start()
        return True

    def pause(self) -> None:
        with self._lock:
            if self._state["running"]:
                self._state["paused"] = True
                self._state["updated_at"] = time.time()

    def resume(self) -> None:
        with self._lock:
            if self._state["running"]:
                self._state["paused"] = False
                self._state["updated_at"] = time.time()

    def stop(self) -> None:
        with self._lock:
            if self._state["running"]:
                self._state["stop_requested"] = True
                self._state["paused"] = False
                self._state["updated_at"] = time.time()

    def snapshot(self) -> dict[str, Any]:
        """Threadsicherer Snapshot, Deals nach % desc sortiert."""
        with self._lock:
            deals_sorted = sorted(self._state["deals"],
                                  key=lambda d: d["pct"], reverse=True)
            return {**self._state, "deals": deals_sorted}

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    def _stop_requested(self) -> bool:
        with self._lock:
            return self._state["stop_requested"]

    def _wait_while_paused(self) -> bool:
        """Liefert ``True``, wenn der Scan abgebrochen werden soll."""
        while True:
            with self._lock:
                if self._state["stop_requested"]:
                    return True
                if not self._state["paused"]:
                    return False
            time.sleep(0.25)

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)
            self._state["updated_at"] = time.time()

    def _worker(self, min_usd: float, max_usd: float, order: str) -> None:
        try:
            sol_rate = self._client.fetch_sol_usd()
        except (requests.RequestException, ValueError, KeyError) as exc:
            with self._lock:
                self._state["error"] = f"Coinbase-Fehler: {exc}"
                self._state["running"] = False
                self._state["done"] = True
                self._state["updated_at"] = time.time()
            return
        self._update(sol_rate=sol_rate)

        try:
            first = self._client.fetch_marketplace_page_with_retry(
                1, config.SCAN_STEP, should_abort=self._stop_requested)
            total_pages = int(first.get("totalPages") or 1)
            rest = list(range(2, total_pages + 1))
            if order == "shuffle":
                random.shuffle(rest)
            pages_order = [1, *rest]
            first_raw = first.get("filterNFtCard") or []

            for page in pages_order:
                if self._wait_while_paused():
                    break
                if page == 1:
                    raw = first_raw
                else:
                    try:
                        data = self._client.fetch_marketplace_page_with_retry(
                            page, config.SCAN_STEP,
                            should_abort=self._stop_requested,
                        )
                    except requests.RequestException as exc:
                        with self._lock:
                            self._state["last_page_error"] = f"Seite {page}: {exc}"
                            self._state["failed_pages"].append(page)
                            self._state["updated_at"] = time.time()
                        time.sleep(1.0)
                        continue
                    raw = data.get("filterNFtCard") or []
                    total_pages = int(data.get("totalPages") or total_pages)
                self._update(page=page, total_pages=total_pages)
                if not raw:
                    continue
                self._process_page(raw, min_usd, max_usd, sol_rate)
                if self._stop_requested():
                    break
        except requests.RequestException as exc:
            with self._lock:
                self._state["error"] = f"Marketplace-Fehler: {exc}"
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["done"] = True
                self._state["updated_at"] = time.time()

    def _process_page(self, raw: Iterable[dict], min_usd: float,
                      max_usd: float, sol_rate: float) -> None:
        for c in raw:
            if self._wait_while_paused():
                return
            n = normalize_card(c)
            with self._lock:
                self._state["scanned"] += 1
            if _is_sticker(n):
                continue
            ask_usd = to_usd(n["price_raw"], n["currency"], sol_rate)
            if ask_usd is None or ask_usd < min_usd or ask_usd > max_usd:
                continue
            with self._lock:
                self._state["in_range"] += 1
            market_usd = n["insured_value"]
            if not market_usd or market_usd <= 0:
                continue
            delta = market_usd - ask_usd
            pct = (delta / market_usd) * 100 if market_usd else 0.0
            deal = _build_deal(n, ask_usd, market_usd, delta, pct)
            with self._lock:
                self._state["deals"].append(deal)
                self._state["matched"] += 1
                self._state["updated_at"] = time.time()


# --------------------------------------------------------------------------- #
# Helfer
# --------------------------------------------------------------------------- #
def _is_sticker(card: dict[str, Any]) -> bool:
    name = (card.get("name") or "").lower()
    card_name = (card.get("card_name") or "").lower()
    return "sticker" in name or "sticker" in card_name


def _build_deal(n: dict[str, Any], ask_usd: float, market_usd: float,
                delta: float, pct: float) -> dict[str, Any]:
    return {
        "name": n["name"], "image": n["image"], "url": n["url"],
        "category": n["category"], "grading": n["grading"],
        "ask_usd": ask_usd, "market_usd": market_usd,
        "delta": delta, "pct": pct,
        "currency": n["currency"], "price_raw": n["price_raw"],
        # Zusatzfelder für Lightbox + Observe-Snapshot:
        "nft": n["nft"], "blockchain": n["blockchain"], "year": n["year"],
        "image_full": n["image_full"], "image_back": n["image_back"],
        "card_name": n["card_name"], "card_number": n["card_number"],
        "language": n["language"], "set": n["set"],
        "grading_company": n["grading_company"],
        "grade_str": n["grade_str"], "grade_num": n["grade_num"],
        "insured_value": n["insured_value"],
        "marketplace": n["marketplace"],
    }
