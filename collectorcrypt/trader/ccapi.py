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

# Endpoint paths (relative to ``config.API_BASE``). Shapes marked VERIFIED were
# confirmed live (probe 2026-06-06, see docs/api.md); the rest remain assumed.
#
# Transport styles (from the frontend bundle's axios wrappers):
#   * REST POST  -> POST <path> with an object body   (marketplace/*)
#   * RPC /v2    -> POST /v2 with {method, params}     (checkListingStatus, ...)
EP_RPC_V2 = "v2"                           # RPC dispatch endpoint
EP_BUY = "marketplace/buy"                 # VERIFIED 201 -> bare base64 tx
EP_MAKE_OFFER = "marketplace/make-offer"   # submit an offer -> returns tx
EP_UPDATE_OFFER = "marketplace/update-offer"  # raise an existing offer -> tx
EP_CANCEL_OFFER = "marketplace/cancel-offer"
EP_LIST = "marketplace/list"               # list a card -> returns tx
EP_UPDATE_LISTING = "marketplace/update-listing"
EP_CANCEL_LISTING = "marketplace/cancel-listing"
EP_ACCEPT_OFFER = "marketplace/accept-offer"
EP_BROADCAST = "marketplace/broadcast"     # broadcast a signed tx
EP_CALC_LISTING_FEE = "calcListingFee"
EP_USER_CARDS = "cards"                    # VERIFIED 200 -> GET cards/{wallet}/
EP_CARD_ACTIVITY = "card-activity"         # VERIFIED 200 -> GET card-activity/{nft}
RPC_CHECK_LISTING_STATUS = "checkListingStatus"   # VERIFIED 200 (RPC method)


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
    def check_listing_status(self, *, nft: str, wallet: str) -> dict[str, Any]:
        """Live listing status for a card. VERIFIED (probe 2026-06-06).

        RPC call: ``POST /v2`` with ``{method, params:{nftAddress, wallet}}``.
        ``params`` is strict — exactly ``nftAddress`` + ``wallet``. Returns
        ``{exists, marketplace, listing}``.
        """
        return self._rpc(RPC_CHECK_LISTING_STATUS,
                         {"nftAddress": nft, "wallet": wallet})

    def calc_listing_fee(self, *, nft: str, price: float,
                         currency: str = "USDC") -> dict[str, Any]:
        # GET with query params is safe to retry. (Unverified shape.)
        return self._request(
            "GET", EP_CALC_LISTING_FEE, auth=True, idempotent=True,
            params={"nftAddress": nft, "price": price, "currency": currency},
        )

    def get_card_activity(self, *, nft: str, day: int = 60) -> dict[str, Any]:
        """Read a card's recent on-chain activity feed. VERIFIED (DevTools 2026-06-07).

        ``GET card-activity/{nft}`` with ``{day, v2}``. Returns a flat,
        newest-first activity log (offers made/cancelled/accepted, listings,
        listing updates), wrapped by the transport as ``{"data": [...]}`` since
        the raw body is a bare JSON array. There is no standing-offers endpoint
        and no clean offer id: the current best incoming bid is reconstructed
        from this feed by :func:`collectorcrypt.trader.holdings.best_active_offer`.
        A read, so it is idempotent and retryable.
        """
        return self._request(
            "GET", f"{EP_CARD_ACTIVITY}/{nft}", auth=True, idempotent=True,
            params={"day": day, "v2": "true"},
        )

    def get_owned_cards(self, *, wallet: str, page: int = 1, step: int = 96,
                        order_by: str = "dateDesc") -> dict[str, Any]:
        """Read a wallet's currently-owned cards. VERIFIED (DevTools 2026-06-07).

        ``GET cards/{wallet}/`` with ``{page, step, orderBy}``. Returns
        ``{totalCards, totalPages, filterNFtCard:[...], ...}`` where each card
        carries ``nftAddress``, ``id``, ``listing`` (object|null), ``listedAt``
        and ``oraclePrice``. The endpoint lists **only cards still owned**: a
        held card that has sold or been transferred away is simply **absent**
        (there is no per-card "Sold" status), so absence from this set is the
        authoritative sold/exited signal. A read, so idempotent and retryable.
        """
        return self._request(
            "GET", f"{EP_USER_CARDS}/{wallet}/", auth=True, idempotent=True,
            params={"page": page, "step": step, "orderBy": order_by},
        )

    # ------------------------------------------------------------------ #
    # Trading writes (state-changing -> NEVER auto-retried)
    # ------------------------------------------------------------------ #
    # The buy/broadcast bodies below are VERIFIED (probe 2026-06-06). The offer/
    # list/cancel bodies remain reverse-engineered assumptions (paths confirmed
    # from the bundle map, bodies not yet probed) and must not drive live spend
    # until verified on a funded test wallet.

    def initiate_buy(self, *, nft: str, price: float, wallet: str,
                     currency: str = "USDC", funding_source: str = "wallet",
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Initiate a direct purchase. VERIFIED HTTP 201 (probe 2026-06-06).

        ``POST marketplace/buy`` with
        ``{currency, nftAddress, price, wallet, fundingSource}``. The response
        body is a **bare base64 ``VersionedTransaction`` string** (no JSON
        envelope) for the buyer to sign locally and broadcast via
        :meth:`broadcast`. ``funding_source`` is ``"wallet"`` or ``"escrow"``.
        """
        body: dict[str, Any] = {
            "currency": currency,
            "nftAddress": nft,
            "price": price,
            "wallet": wallet,
            "fundingSource": funding_source,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_BUY, auth=True, json=body)

    def make_offer(self, *, nft: str, card_id: str, price: float,
                   wallet: str, currency: str = "USDC",
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Submit a standing offer (bid) below the ask. Returns a tx to sign.

        VERIFIED body (DevTools capture 2026-06-07):
        ``{cardId, currency, nftAddress, price, wallet}``. ``cardId`` is the
        card's internal CC id (raw card ``id``, e.g. ``"2024122019C5785"``) and
        is **required** — sending only ``{nftAddress, price, currency}`` is
        rejected with 400. The response is a bare base64 ``VersionedTransaction``
        string (same envelope as ``buy``) for the bidder to sign and broadcast.
        State-changing, so it is never auto-retried.
        """
        body: dict[str, Any] = {
            "cardId": card_id,
            "currency": currency,
            "nftAddress": nft,
            "price": price,
            "wallet": wallet,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_MAKE_OFFER, auth=True, json=body)

    def update_offer(self, *, nft: str, price: float, wallet: str,
                     currency: str = "USDC",
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Raise the price of an existing standing offer (offer penetration).

        VERIFIED body (DevTools capture 2026-06-07):
        ``{buyer, currency, nftAddress, price, wallet}`` where ``buyer`` and
        ``wallet`` are both the bidder's address. This is a real offer **edit**
        (a single re-notification of the owner) rather than a cancel+remake,
        and returns a bare base64 transaction to sign and broadcast.
        State-changing, so it is never auto-retried.
        """
        body: dict[str, Any] = {
            "buyer": wallet,
            "currency": currency,
            "nftAddress": nft,
            "price": price,
            "wallet": wallet,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_UPDATE_OFFER, auth=True, json=body)

    def create_listing(self, *, nft: str, card_id: str, price: float,
                       wallet: str, currency: str = "USDC",
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """List an owned card for sale. Returns a tx to sign (the exit side).

        VERIFIED body (DevTools capture 2026-06-08):
        ``{cardId, currency, nftAddress, price, wallet}`` — like make-offer it
        needs the card's internal CC id (``cardId``) plus the seller ``wallet``.
        Returns a bare base64 transaction to sign and broadcast.
        State-changing, never auto-retried.
        """
        body = {
            "cardId": card_id,
            "currency": currency,
            "nftAddress": nft,
            "price": price,
            "wallet": wallet,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_LIST, auth=True, json=body)

    def cancel_listing(self, *, nft: str, wallet: str, currency: str = "USDC",
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Withdraw an active listing. Returns a tx to sign (no funds move).

        VERIFIED body (DevTools capture 2026-06-08):
        ``{coin, seller, tokenMint, wallet}`` — the currency field is ``coin``,
        the mint is ``tokenMint`` and there is **no** listing id; the listing is
        identified by ``tokenMint`` + ``wallet`` (``seller`` == our wallet).
        Returns a bare base64 transaction to sign and broadcast.
        State-changing, never auto-retried.
        """
        body = {
            "coin": currency,
            "seller": wallet,
            "tokenMint": nft,
            "wallet": wallet,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_CANCEL_LISTING, auth=True, json=body)

    def cancel_offer(self, *, nft: str, wallet: str, currency: str = "USDC",
                     keep_in_escrow: bool = False,
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Withdraw a standing offer; the escrowed funds are refunded.

        VERIFIED body (DevTools capture 2026-06-07):
        ``{coin, keepInEscrow, nftAddress, wallet}`` — note the currency field
        is ``coin`` (not ``currency``) and there is **no** offer id; the offer
        is identified by ``nftAddress`` + ``wallet``. Returns a bare base64
        transaction to sign and broadcast. State-changing, never auto-retried.
        """
        body: dict[str, Any] = {
            "coin": currency,
            "keepInEscrow": keep_in_escrow,
            "nftAddress": nft,
            "wallet": wallet,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_CANCEL_OFFER, auth=True, json=body)

    def update_listing(self, *, nft: str, price: float, wallet: str,
                       currency: str = "USDC",
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Change the price of a live listing (markdown). VERIFIED (DevTools 2026-06-07).

        ``POST marketplace/update-listing`` with
        ``{coin, newPrice, seller, tokenMint, wallet}`` — ``seller`` and
        ``wallet`` are both our own address. Returns a **bare base64
        ``VersionedTransaction`` string** to sign locally and broadcast via
        :meth:`broadcast`. State-changing, so it is never auto-retried.
        """
        body: dict[str, Any] = {
            "coin": currency,
            "newPrice": price,
            "seller": wallet,
            "tokenMint": nft,
            "wallet": wallet,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_UPDATE_LISTING, auth=True, json=body)

    def accept_offer(self, *, nft: str, buyer: str, price: float, wallet: str,
                     currency: str = "USDC",
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Accept an incoming bid on an owned card. VERIFIED (DevTools 2026-06-07).

        ``POST marketplace/accept-offer`` with
        ``{buyer, currency, nftAddress, price, wallet}``. An offer is referenced
        by ``buyer`` + ``price`` + ``nftAddress`` (there is no offer id);
        ``wallet`` is our own (seller) address. Returns a **bare base64
        ``VersionedTransaction`` string** to sign locally and broadcast via
        :meth:`broadcast` to settle the sale. State-changing, so it is never
        auto-retried.
        """
        body: dict[str, Any] = {
            "buyer": buyer,
            "currency": currency,
            "nftAddress": nft,
            "price": price,
            "wallet": wallet,
        }
        if extra:
            body.update(extra)
        return self._request("POST", EP_ACCEPT_OFFER, auth=True, json=body)

    def broadcast(self, *, signed_tx: str, wallet: str = "", nft: str = "",
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Broadcast a locally-signed transaction.

        VERIFIED body (from the bundle): ``{signedTransaction, wallet,
        nftAddress?}``. VERIFIED response (DevTools capture 2026-06-07):
        ``{"success": true, "signature": "<sig>", "message": "..."}``. This is
        the only step that finalises a trade on-chain, so it is never retried
        automatically.
        """
        body: dict[str, Any] = {"signedTransaction": signed_tx}
        if wallet:
            body["wallet"] = wallet
        if nft:
            body["nftAddress"] = nft
        if extra:
            body.update(extra)
        return self._request("POST", EP_BROADCAST, auth=True, json=body)

    # ------------------------------------------------------------------ #
    # RPC transport (POST /v2 with {method, params})
    # ------------------------------------------------------------------ #
    def _rpc(self, method: str, params: dict[str, Any], *,
             idempotent: bool = True) -> dict[str, Any]:
        """Dispatch an RPC-style call (``POST /v2`` with ``{method, params}``).

        Read RPCs (e.g. ``checkListingStatus``) are idempotent and retryable.
        """
        return self._request("POST", EP_RPC_V2, auth=True,
                             idempotent=idempotent,
                             json={"method": method, "params": params})

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
            if isinstance(body, dict):
                return body
            # Some endpoints (e.g. marketplace/buy) return a bare value rather
            # than a JSON object: the response body *is* the base64 unsigned
            # transaction string. Fall back to the raw text so the caller can
            # read it from the ``data`` envelope.
            if body is None:
                text = resp.text.strip()
                return {"data": text} if text else {"data": None}
            return {"data": body}

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
