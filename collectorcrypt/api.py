"""HTTP client for CollectorCrypt + Coinbase, including cache and retry."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

import requests

from . import config


_DEFAULT_HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "application/json",
}


class CCClient:
    """Thin wrapper around ``requests`` with in-memory cache and retry."""

    def __init__(self, *, session: requests.Session | None = None,
                 cache_ttl: float = config.CACHE_TTL_SECONDS,
                 cache_max_entries: int = config.CACHE_MAX_ENTRIES) -> None:
        self._session = session or requests.Session()
        self._session.headers.update(_DEFAULT_HEADERS)
        self._cache_ttl = cache_ttl
        self._cache_max_entries = max(1, int(cache_max_entries))
        self._cache: "OrderedDict[tuple, tuple[float, Any]]" = OrderedDict()
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Marketplace
    # ------------------------------------------------------------------ #
    def fetch_marketplace_page(self, page: int, step: int = config.DEFAULT_STEP,
                               search: str = "", category: str = "") -> dict[str, Any]:
        key = ("marketplace", page, step, search, category)
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
        category: str = "",
    ) -> dict[str, Any]:
        """Like :meth:`fetch_marketplace_page`, but with backoff on 403/429/5xx."""
        should_abort = should_abort or (lambda: False)
        last_exc: Exception | None = None
        for delay in (*config.RETRY_DELAYS, None):
            try:
                return self.fetch_marketplace_page(page, step, "", category)
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
                # Abort requested – propagate cleanly.
                raise last_exc  # type: ignore[misc]
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------ #
    # Wallet / Profile
    # ------------------------------------------------------------------ #
    def fetch_wallet_cards(self, wallet: str, page: int = 1,
                           step: int = config.DEFAULT_STEP) -> dict[str, Any]:
        """Cards owned by a Solana wallet. Returns the raw API response or
        an empty stub when the wallet is unknown (404)."""
        key = ("wallet", wallet, page, step)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        url = f"{config.API_BASE}/cards/{wallet}"
        params = {"page": page, "step": step}
        r = self._session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 404:
            data = {"totalCards": 0, "total": 0, "findTotal": 0, "totalPages": 0,
                    "insuredValueSum": "0", "cardsQtyByCategory": {},
                    "filterNFtCard": [], "_notFound": True}
            self._cache_set(key, data)
            return data
        r.raise_for_status()
        data = r.json()
        self._cache_set(key, data)
        return data

    # ------------------------------------------------------------------ #
    # Single card
    # ------------------------------------------------------------------ #
    def fetch_card(self, nft: str) -> dict[str, Any] | None:
        """``None`` on 404 (no longer listed)."""
        url = config.PUBLIC_NFT_URL_TEMPLATE.format(nft=nft)
        r = self._session.get(url, timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ #
    # SOL/USD spot price
    # ------------------------------------------------------------------ #
    def fetch_sol_usd(self) -> float:
        r = self._session.get(config.COINBASE_SOL_URL, timeout=15)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])

    # ------------------------------------------------------------------ #
    # Internals
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
                self._cache.move_to_end(key)  # mark as recently used
                return hit[1]
        return None

    def _cache_set(self, key: tuple, value: Any) -> None:
        now = time.time()
        with self._cache_lock:
            # Drop entries whose TTL has already elapsed so stale data does not
            # linger (TTL is otherwise only checked on read).
            expired = [k for k, (ts, _) in self._cache.items()
                       if (now - ts) >= self._cache_ttl]
            for k in expired:
                del self._cache[k]
            self._cache[key] = (now, value)
            self._cache.move_to_end(key)
            # Bound total size: evict the least-recently-used entries.
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)


def _sleep_with_abort(seconds: float, should_abort: Callable[[], bool]) -> bool:
    """Sleep for ``seconds`` seconds, checking ``should_abort`` every 0.5s.

    Returns ``True`` if the sleep ended normally, otherwise ``False``
    (abort requested)."""
    end = time.time() + seconds
    while time.time() < end:
        if should_abort():
            return False
        time.sleep(0.5)
    return True
