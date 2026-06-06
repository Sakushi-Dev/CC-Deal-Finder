"""Authenticated CollectorCrypt trading client.

This is the technical substrate for the live trading flows (Buy / Offer / List /
Broadcast). It deliberately stops *at* the integration boundary: it can build,
authenticate, send and interpret the relevant requests, but it is **not** wired
into live execution yet (that is ETAPPE 5) and every trading method documents
exactly which parts are reverse-engineered assumptions versus confirmed.

What this layer provides
------------------------
* **Clean public/authenticated split.** Public reads stay in
  :class:`~collectorcrypt.api.CCClient`. Anything requiring a Privy session
  goes through :class:`CCTradingClient`, which always resolves a valid
  :class:`~collectorcrypt.trader.auth.AuthSession` first (and refuses, loudly,
  if it cannot).
* **Structured errors.** Every failure maps to a typed
  :class:`CCApiError` subclass so callers can branch on *auth* vs *rate limit*
  vs *client* vs *server* vs *network* without parsing strings.
* **Safe retry only.** Idempotent reads and explicitly-safe operations are
  retried with backoff on ``429``/``5xx`` (honouring ``Retry-After``). State-
  changing trading calls (buy/offer/list/broadcast) are **never** retried
  automatically — a silent retry could double-spend. Their idempotency is the
  caller's responsibility via the persisted ``client_order_id``.
* **Redacted logging.** Authorization headers, tokens, signatures and secrets
  are masked before anything is logged. No secret ever reaches a log sink.

Nothing here signs a transaction or touches a private key — signing stays in
:class:`~collectorcrypt.trader.wallet.Wallet` and is invoked by the executor.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping

import requests

from .. import config as app_config
from .auth import NullSessionProvider, SessionProvider

logger = logging.getLogger("collectorcrypt.trader.ccapi")

# Endpoint paths (relative to ``config.API_BASE``). Sourced from docs/api.md;
# methods/payloads marked "assumed" are reverse-engineered and unverified.
EP_USERS_ME = "api/v1/users/me"
EP_ACCOUNT_LISTINGS = "account/{account_id}/listings"
EP_ACCOUNT_OFFERS_MADE = "account/{account_id}/offers-made"
EP_BUY = "marketplace/buy"                 # initiate purchase -> returns tx
EP_MAKE_OFFER = "marketplace/make-offer"   # submit an offer -> returns tx
EP_CANCEL_OFFER = "marketplace/cancel-offer"
EP_LIST = "marketplace/list"               # list a card -> returns tx
EP_UPDATE_LISTING = "marketplace/update-listing"
EP_CANCEL_LISTING = "marketplace/cancel-listing"
EP_BROADCAST = "marketplace/broadcast"     # broadcast a signed tx
EP_CALC_LISTING_FEE = "calcListingFee"
EP_CHECK_LISTING_STATUS = "checkListingStatus"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class CCApiError(RuntimeError):
    """Base class for all CollectorCrypt API failures."""

    def __init__(self, message: str, *, status: int | None = None,
                 path: str = "", body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.body = body


class CCAuthError(CCApiError):
    """Authentication/authorization failed (no session, 401, 403)."""


class CCRateLimitError(CCApiError):
    """The API asked us to slow down (429). Carries the retry delay."""

    def __init__(self, message: str, *, retry_after: float = 0.0,
                 **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class CCClientError(CCApiError):
    """A 4xx the caller is responsible for (bad request, not found, conflict)."""


class CCServerError(CCApiError):
    """A 5xx on CollectorCrypt's side."""


class CCNetworkError(CCApiError):
    """The request never produced an HTTP response (timeout, DNS, reset)."""


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
_SENSITIVE_KEYS = re.compile(
    r"(authorization|token|secret|bearer|signature|signedtransaction|"
    r"signedtx|privatekey|password|cookie)",
    re.IGNORECASE,
)


def _mask(value: str) -> str:
    if not value:
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-2:]}"


def redact(data: Any) -> Any:
    """Return a copy of ``data`` with sensitive values masked, for logging.

    Recurses through dicts/lists. Any key whose name looks sensitive has its
    value masked; long bare strings are left untouched (only keyed secrets are
    masked) to keep diagnostics useful.
    """
    if isinstance(data, Mapping):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if _SENSITIVE_KEYS.search(str(k)):
                out[k] = _mask(v) if isinstance(v, str) else "***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(data, (list, tuple)):
        return [redact(v) for v in data]
    return data


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class CCTradingClient:
    """Authenticated transport for CollectorCrypt trading endpoints."""

    def __init__(self, *, session_provider: SessionProvider | None = None,
                 base_url: str = app_config.API_BASE,
                 http: requests.Session | None = None,
                 timeout: float = app_config.REQUEST_TIMEOUT,
                 max_retries: int = 3) -> None:
        self._auth = session_provider or NullSessionProvider()
        self._base = base_url.rstrip("/")
        self._http = http or requests.Session()
        self._http.headers.setdefault("User-Agent", app_config.USER_AGENT)
        self._http.headers.setdefault("Accept", "application/json")
        self._timeout = timeout
        self._max_retries = max(0, max_retries)

    # ------------------------------------------------------------------ #
    # Authenticated reads (safe, idempotent -> retryable)
    # ------------------------------------------------------------------ #
    def me(self) -> dict[str, Any]:
        """Profile of the authenticated user. Confirms the session works."""
        return self._request("GET", EP_USERS_ME, auth=True, idempotent=True)

    def account_listings(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "GET", EP_ACCOUNT_LISTINGS.format(account_id=account_id),
            auth=True, idempotent=True,
        )

    def account_offers_made(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "GET", EP_ACCOUNT_OFFERS_MADE.format(account_id=account_id),
            auth=True, idempotent=True,
        )

    def check_listing_status(self, listing_id: str) -> dict[str, Any]:
        return self._request(
            "GET", EP_CHECK_LISTING_STATUS, auth=True, idempotent=True,
            params={"id": listing_id},
        )

    def calc_listing_fee(self, *, nft: str, price: float,
                         currency: str = "USDC") -> dict[str, Any]:
        # GET with query params is safe to retry.
        return self._request(
            "GET", EP_CALC_LISTING_FEE, auth=True, idempotent=True,
            params={"nftAddress": nft, "price": price, "currency": currency},
        )

    # ------------------------------------------------------------------ #
    # Trading writes (state-changing -> NEVER auto-retried)
    # ------------------------------------------------------------------ #
    # NOTE: Request bodies below are reverse-engineered assumptions from the
    # frontend bundle (see docs/api.md). They are sent verbatim and the raw
    # response is returned for the executor (ETAPPE 5) to interpret. Until the
    # flow is verified on a funded test wallet these must not drive live spend.

    def initiate_buy(self, *, nft: str, price: float, currency: str = "USDC",
                     receipt_id: str = "", extra: dict[str, Any] | None = None
                     ) -> dict[str, Any]:
        """Initiate a direct purchase. Expected to return an unsigned tx payload.

        ASSUMPTION: ``marketplace/buy`` accepts the listing's nft address, the
        agreed price/currency and the listing ``receiptId``, and responds with a
        serialized Solana transaction for the buyer to sign and then broadcast
        via :meth:`broadcast`.
        """
        body = {"nftAddress": nft, "price": price, "currency": currency}
        if receipt_id:
            body["receiptId"] = receipt_id
        if extra:
            body.update(extra)
        return self._request("POST", EP_BUY, auth=True, json=body)

    def make_offer(self, *, nft: str, price: float, currency: str = "USDC",
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Submit a standing offer (bid) below the ask. Returns a tx to sign.

        ASSUMPTION: ``marketplace/make-offer`` mirrors ``buy`` but records a bid
        the seller may later accept.
        """
        body = {"nftAddress": nft, "price": price, "currency": currency}
        if extra:
            body.update(extra)
        return self._request("POST", EP_MAKE_OFFER, auth=True, json=body)

    def create_listing(self, *, nft: str, price: float, currency: str = "USDC",
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """List an owned card for sale. Returns a tx to sign (the exit side)."""
        body = {"nftAddress": nft, "price": price, "currency": currency}
        if extra:
            body.update(extra)
        return self._request("POST", EP_LIST, auth=True, json=body)

    def cancel_listing(self, *, nft: str = "", listing_id: str = ""
                       ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if nft:
            body["nftAddress"] = nft
        if listing_id:
            body["id"] = listing_id
        return self._request("POST", EP_CANCEL_LISTING, auth=True, json=body)

    def cancel_offer(self, *, offer_id: str) -> dict[str, Any]:
        return self._request("POST", EP_CANCEL_OFFER, auth=True,
                             json={"id": offer_id})

    def broadcast(self, *, signed_tx: str,
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Broadcast a locally-signed transaction.

        ASSUMPTION: ``marketplace/broadcast`` accepts the base64/base58 signed
        transaction and returns the on-chain signature plus a receipt. This is
        the only step that finalises a trade on-chain, so it is never retried.
        """
        body = {"signedTransaction": signed_tx}
        if extra:
            body.update(extra)
        return self._request("POST", EP_BROADCAST, auth=True, json=body)

    # ------------------------------------------------------------------ #
    # Core transport
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, *, auth: bool,
                 json: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None,
                 idempotent: bool = False) -> dict[str, Any]:
        """Send one request, applying auth, retries and error mapping.

        ``idempotent`` gates automatic retries: only safe reads (and explicitly
        safe operations) set it. State-changing trading calls leave it ``False``
        so a transient failure surfaces to the caller instead of being silently
        re-sent.
        """
        url = f"{self._base}/{path.lstrip('/')}"
        attempts = (self._max_retries + 1) if idempotent else 1
        last_exc: CCApiError | None = None

        for attempt in range(1, attempts + 1):
            headers: dict[str, str] = {}
            if auth:
                # Resolve a valid session per attempt so a refresh between
                # retries is picked up. Provider raises CCAuthError if it can't.
                headers.update(self._auth.get_session().auth_header())

            logger.debug(
                "CC %s %s params=%s body=%s (attempt %d/%d)",
                method, path, redact(params or {}), redact(json or {}),
                attempt, attempts,
            )
            try:
                resp = self._http.request(
                    method, url, params=params, json=json, headers=headers,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                last_exc = CCNetworkError(f"{method} {path} failed: {exc}",
                                          path=path)
                if attempt < attempts and _backoff(attempt):
                    continue
                raise last_exc from exc

            try:
                return self._handle_response(resp, method, path)
            except CCAuthError:
                # A 401 means our session is stale: drop it so the next attempt
                # (if any) rebuilds. Never retry a write on auth failure.
                self._auth.invalidate()
                raise
            except CCRateLimitError as exc:
                last_exc = exc
                if attempt < attempts:
                    _sleep(exc.retry_after or _backoff_delay(attempt))
                    continue
                raise
            except CCServerError as exc:
                last_exc = exc
                if attempt < attempts and _backoff(attempt):
                    continue
                raise

        # Exhausted retries on a retryable error.
        assert last_exc is not None  # for type-checkers
        raise last_exc

    def _handle_response(self, resp: requests.Response, method: str,
                         path: str) -> dict[str, Any]:
        status = resp.status_code
        body = _safe_json(resp)

        if 200 <= status < 300:
            logger.debug("CC %s %s -> %d OK", method, path, status)
            return body if isinstance(body, dict) else {"data": body}

        msg = _error_message(body) or resp.text[:200]
        logger.warning("CC %s %s -> %d %s", method, path, status,
                       redact(body) if isinstance(body, (dict, list)) else msg)

        if status in (401, 403):
            raise CCAuthError(f"Auth failed ({status}): {msg}",
                              status=status, path=path, body=body)
        if status == 429:
            raise CCRateLimitError(
                f"Rate limited ({status}): {msg}",
                retry_after=_retry_after(resp), status=status, path=path,
                body=body,
            )
        if 400 <= status < 500:
            raise CCClientError(f"Client error ({status}): {msg}",
                                status=status, path=path, body=body)
        raise CCServerError(f"Server error ({status}): {msg}",
                            status=status, path=path, body=body)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _error_message(body: Any) -> str:
    if isinstance(body, Mapping):
        for key in ("message", "error", "detail", "msg"):
            val = body.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def _retry_after(resp: requests.Response) -> float:
    raw = resp.headers.get("Retry-After", "")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _backoff_delay(attempt: int) -> float:
    delays = app_config.RETRY_DELAYS
    idx = min(attempt - 1, len(delays) - 1)
    return float(delays[idx])


def _backoff(attempt: int) -> bool:
    _sleep(_backoff_delay(attempt))
    return True


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
