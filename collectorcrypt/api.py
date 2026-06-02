"""HTTP-Client für CollectorCrypt + Coinbase, inkl. Cache und Retry."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests

from . import config


_DEFAULT_HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "application/json",
}


class CCClient:
    """Dünner Wrapper um ``requests`` mit In-Memory-Cache und Retry."""

    def __init__(self, *, session: requests.Session | None = None,
                 cache_ttl: float = config.CACHE_TTL_SECONDS) -> None:
        self._session = session or requests.Session()
        self._session.headers.update(_DEFAULT_HEADERS)
        self._cache_ttl = cache_ttl
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Marketplace
    # ------------------------------------------------------------------ #
    def fetch_marketplace_page(self, page: int, step: int = config.DEFAULT_STEP,
                               search: str = "") -> dict[str, Any]:
        key = ("marketplace", page, step, search)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        params = {"page": page, "step": step, "cardType": "Card"}
        if search:
            params["search"] = search
        data = self._get_json(config.MARKETPLACE_URL, params=params)
        self._cache_set(key, data)
        return data

    def fetch_marketplace_page_with_retry(
        self, page: int, step: int, *, should_abort: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Wie :meth:`fetch_marketplace_page`, aber mit Backoff bei 403/429/5xx."""
        should_abort = should_abort or (lambda: False)
        last_exc: Exception | None = None
        for delay in (*config.RETRY_DELAYS, None):
            try:
                return self.fetch_marketplace_page(page, step, "")
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                last_exc = exc
                if code not in config.RETRY_STATUSES or delay is None:
                    raise
            except requests.RequestException as exc:
                last_exc = exc
                if delay is None:
                    raise
            if not _sleep_with_abort(delay, should_abort):
                # Abort gewünscht – sauber durchreichen.
                raise last_exc  # type: ignore[misc]
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------ #
    # Einzelne Karte
    # ------------------------------------------------------------------ #
    def fetch_card(self, nft: str) -> dict[str, Any] | None:
        """``None`` bei 404 (nicht mehr gelistet)."""
        url = config.PUBLIC_NFT_URL_TEMPLATE.format(nft=nft)
        r = self._session.get(url, timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ #
    # SOL/USD-Spot
    # ------------------------------------------------------------------ #
    def fetch_sol_usd(self) -> float:
        r = self._session.get(config.COINBASE_SOL_URL, timeout=15)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #
    def _get_json(self, url: str, *, params: dict | None = None) -> Any:
        r = self._session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _cache_get(self, key: tuple) -> Any | None:
        now = time.time()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and (now - hit[0]) < self._cache_ttl:
                return hit[1]
        return None

    def _cache_set(self, key: tuple, value: Any) -> None:
        with self._cache_lock:
            self._cache[key] = (time.time(), value)


def _sleep_with_abort(seconds: float, should_abort: Callable[[], bool]) -> bool:
    """Schläft ``seconds`` Sekunden, prüft alle 0.5s ``should_abort``.

    Liefert ``True``, wenn das Schlafen normal endete, sonst ``False``
    (Abort gewünscht)."""
    end = time.time() + seconds
    while time.time() < end:
        if should_abort():
            return False
        time.sleep(0.5)
    return True
