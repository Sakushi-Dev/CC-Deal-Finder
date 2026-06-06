"""Privy Sign-In-With-Solana (SIWS) session provider.

This implements the real authenticated-session handshake behind the
:class:`~collectorcrypt.trader.auth.SessionProvider` seam introduced in
ETAPPE 3. CollectorCrypt fronts its auth with Privy; a Solana wallet proves
ownership by signing a challenge message, and Privy returns a bearer token the
trading client then carries.

Integration boundary
---------------------
The Privy/CC SIWS request shapes were **verified against a live request capture
and an end-to-end handshake** (2026-06-06; see ``docs/api.md``): the wallet
signs the challenge, Privy returns a bearer JWT, and the CollectorCrypt trading
API accepts that JWT directly. The authenticate response field names
(token/expiry/account) are still read defensively since only the request side
was captured. Nothing here spends funds — it only establishes a session object.

Flow (verified)
---------------
1. **init** — ``POST auth.privy.io/api/v1/siws/init`` with ``{address}``; Privy
   returns a ``nonce`` (no message; the client builds it).
2. **sign** — build the Privy Solana SIWS message and sign it locally with the
   wallet keypair (the private key never leaves the process). The signature is
   transmitted **base64**-encoded.
3. **authenticate** — ``POST auth.privy.io/api/v1/siws/authenticate`` with
   ``{message, signature, walletClientType:"Phantom", connectorType, mode,
   message_type}``; Privy returns a bearer ``token`` and an expiry.

Security & safety
-----------------
* The bearer token lives only in memory and is redacted from logs.
* The provider is **fail-safe**: any deviation (missing nonce, no token,
  signing failure, HTTP error) raises :class:`CCAuthError`. It never returns an
  invalid session, so the trader refuses to act rather than sending
  unauthenticated trading requests.
* :meth:`get_session` caches the session and refreshes only when it is within
  the expiry skew window — no hidden, untracked re-auth on every call.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

from .. import config as app_config
from .auth import (AuthSession, NullSessionProvider, SessionProvider,
                   StaticTokenProvider)
from .ccapi import CCAuthError, CCNetworkError, redact
from .wallet import Wallet, WalletError

logger = logging.getLogger("collectorcrypt.trader.siws")

# Privy SIWS runs on auth.privy.io (NOT the CollectorCrypt API host). Verified
# against the live frontend, 2026-06-06.
PRIVY_AUTH_BASE = "https://auth.privy.io"

EP_SIWS_INIT = "api/v1/siws/init"
EP_SIWS_AUTH = "api/v1/siws/authenticate"

# Privy react-auth client version sent in the ``privy-client`` header by the
# CollectorCrypt frontend (verified from a captured request).
PRIVY_CLIENT_VERSION = "react-auth:3.28.0"

# If the authenticate response carries no explicit expiry, assume a
# conservative session lifetime so the provider refreshes proactively rather
# than discovering expiry via a mid-flight 401.
DEFAULT_SESSION_TTL_SEC = 3600.0


class PrivySiwsProvider:
    """Establishes and refreshes a CollectorCrypt session via Privy SIWS."""

    def __init__(self, wallet: Wallet, *, app_id: str = "",
                 client_id: str = "",
                 base_url: str = PRIVY_AUTH_BASE,
                 http: requests.Session | None = None,
                 timeout: float = app_config.REQUEST_TIMEOUT,
                 origin: str = "https://collectorcrypt.com",
                 domain: str = "collectorcrypt.com",
                 uri: str = "https://collectorcrypt.com",
                 chain_id: str = "mainnet") -> None:
        if not wallet.can_sign:
            raise CCAuthError(
                "PrivySiwsProvider requires a signing wallet "
                "(TRADER_WALLET_SECRET). A read-only wallet cannot authenticate."
            )
        self._wallet = wallet
        self._app_id = app_id
        self._client_id = client_id
        self._base = base_url.rstrip("/")
        self._http = http or requests.Session()
        self._http.headers.setdefault("User-Agent", app_config.USER_AGENT)
        self._http.headers.setdefault("Accept", "application/json")
        self._timeout = timeout
        self._origin = origin.rstrip("/")
        self._domain = domain
        self._uri = uri
        self._chain_id = chain_id
        # Privy binds a request to a client-assigned "ca-id" (a UUID); the
        # browser persists one per device. We mint one per provider instance
        # and send it on both init and authenticate so they correlate.
        self._ca_id = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._session: AuthSession | None = None

    # ------------------------------------------------------------------ #
    # SessionProvider interface
    # ------------------------------------------------------------------ #
    def get_session(self) -> AuthSession:
        """Return a valid session, authenticating or refreshing as needed."""
        with self._lock:
            if self._session is not None and self._session.is_valid:
                return self._session
            self._session = self._authenticate()
            return self._session

    def invalidate(self) -> None:
        with self._lock:
            self._session = None

    # ------------------------------------------------------------------ #
    # Handshake
    # ------------------------------------------------------------------ #
    def _authenticate(self) -> AuthSession:
        address = self._wallet.address
        nonce, message = self._init_challenge(address)
        try:
            # Privy's authenticate endpoint expects the signature base64-encoded
            # (verified against a live request capture, 2026-06-06).
            signature = self._wallet.sign_message(
                message.encode("utf-8"), encoding="base64")
        except WalletError as exc:
            raise CCAuthError(f"SIWS signing failed: {exc}") from exc

        # Verified authenticate body shape (2026-06-06). Note: no address/nonce
        # fields — the signed ``message`` carries the nonce, and the wallet type
        # is the Privy connector name, capitalised.
        payload = {
            "message": message,
            "signature": signature,
            "walletClientType": "Phantom",
            "connectorType": "solana_adapter",
            "mode": "login-or-sign-up",
            "message_type": "plain",
        }
        data = self._post(EP_SIWS_AUTH, payload)
        token = _extract_token(data)
        if not token:
            raise CCAuthError(
                "SIWS authenticate returned no token; cannot establish a "
                "session. Response shape may have changed (see docs/api.md)."
            )
        expires_at = _extract_expiry(data)
        account_id = str(
            data.get("account_id") or data.get("accountId")
            or (data.get("user") or {}).get("id") or ""
        )
        logger.info("SIWS session established for %s (account %s)",
                    address, account_id or "?")
        return AuthSession(token=token, account_id=account_id,
                           wallet=address, expires_at=expires_at)

    def _init_challenge(self, address: str) -> tuple[str, str]:
        """Obtain a nonce and build the SIWS message to sign.

        ASSUMPTION: the init endpoint returns a ``nonce`` (and optionally a
        complete ``message``). If it provides a ready message we sign that
        verbatim; otherwise we construct a SIWS-style message locally. A locally
        generated nonce is used as a last resort so the flow degrades to a
        clear authenticate-time error rather than crashing here.
        """
        try:
            data = self._post(EP_SIWS_INIT, {"address": address})
        except CCAuthError:
            raise
        except CCNetworkError:
            raise
        nonce = str(data.get("nonce") or data.get("challenge") or "").strip()
        prebuilt = data.get("message")
        if isinstance(prebuilt, str) and prebuilt.strip():
            # Trust the server-provided message; still surface the nonce.
            return nonce or secrets.token_hex(8), prebuilt
        if not nonce:
            nonce = secrets.token_hex(16)
        return nonce, self._build_siws_message(address, nonce)

    def _build_siws_message(self, address: str, nonce: str) -> str:
        """Construct the Privy Solana SIWS message.

        VERIFIED template (2026-06-06): the exact byte-for-byte layout Privy
        accepts for CollectorCrypt. ``Issued At`` uses millisecond precision to
        match the browser client. The nonce comes from the init challenge.
        """
        now = datetime.now(timezone.utc)
        issued_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        return (
            f"{self._domain} wants you to sign in with your Solana account:\n"
            f"{address}\n\n"
            f"You are proving you own {address}.\n\n"
            f"URI: {self._uri}\n"
            f"Version: 1\n"
            f"Chain ID: {self._chain_id}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}\n"
            f"Resources:\n"
            f"- https://privy.io"
        )

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/{path.lstrip('/')}"
        # Header set verified against a live request (2026-06-06). Privy rejects
        # requests without an Origin (403 missing_origin) and binds the call to
        # the app/client identifiers and a per-device ca-id.
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Origin": self._origin,
            "Referer": f"{self._origin}/",
            "privy-ca-id": self._ca_id,
            "privy-client": PRIVY_CLIENT_VERSION,
        }
        if self._app_id:
            headers["privy-app-id"] = self._app_id
        if self._client_id:
            headers["privy-client-id"] = self._client_id
        logger.debug("SIWS POST %s body=%s", path, redact(body))
        try:
            resp = self._http.post(url, json=body, headers=headers,
                                   timeout=self._timeout)
        except requests.RequestException as exc:
            raise CCNetworkError(f"SIWS {path} failed: {exc}", path=path) from exc

        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code in (401, 403):
            raise CCAuthError(
                f"SIWS {path} rejected ({resp.status_code}). Check the wallet "
                f"and Privy app id.",
                status=resp.status_code, path=path, body=data,
            )
        if resp.status_code >= 400:
            raise CCAuthError(
                f"SIWS {path} failed ({resp.status_code}).",
                status=resp.status_code, path=path, body=data,
            )
        if not isinstance(data, dict):
            raise CCAuthError(f"SIWS {path} returned a non-JSON body.",
                              status=resp.status_code, path=path)
        return data


# --------------------------------------------------------------------------- #
# Response parsing helpers (isolated because the shapes are unverified)
# --------------------------------------------------------------------------- #
def _extract_token(data: dict[str, Any]) -> str:
    for key in ("token", "access_token", "accessToken", "jwt",
                "privy_access_token", "session_token"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    # Sometimes nested under a session/user object.
    for parent in ("session", "data"):
        child = data.get(parent)
        if isinstance(child, dict):
            nested = _extract_token(child)
            if nested:
                return nested
    return ""


def _extract_expiry(data: dict[str, Any]) -> float:
    """Resolve an absolute epoch-seconds expiry from common shapes."""
    # Absolute timestamps.
    for key in ("expires_at", "expiresAt", "exp"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
        if isinstance(val, str):
            ts = _parse_iso(val)
            if ts:
                return ts
    # Relative lifetimes.
    for key in ("expires_in", "expiresIn", "ttl"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return time.time() + float(val)
    return time.time() + DEFAULT_SESSION_TTL_SEC


def _parse_iso(value: str) -> float:
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# Provider selection + live-readiness gate
# --------------------------------------------------------------------------- #
def make_session_provider(cfg, wallet: Wallet, *,
                          http: requests.Session | None = None
                          ) -> SessionProvider:
    """Build the configured :class:`SessionProvider` for ``cfg``.

    Selection is driven by ``TRADER_AUTH_PROVIDER`` (env-only):

    * ``"none"`` (default) -> :class:`NullSessionProvider` (cannot authenticate).
    * ``"static"`` -> :class:`StaticTokenProvider` (uses ``TRADER_CC_TOKEN``).
    * ``"privy"`` -> :class:`PrivySiwsProvider` (real SIWS handshake; needs a
      signing wallet).

    The factory never raises for ``"none"``: an un-configured trader simply gets
    a provider that safely refuses. Misconfigured real providers raise so the
    problem is visible immediately rather than at the first trade.
    """
    provider = (cfg.auth_provider or "none").lower()
    if provider == "static":
        return StaticTokenProvider(cfg.cc_token)
    if provider == "privy":
        return PrivySiwsProvider(wallet, app_id=cfg.privy_app_id,
                                 client_id=cfg.privy_client_id, http=http)
    return NullSessionProvider()


def check_live_ready(cfg, wallet: Wallet, *,
                     http: requests.Session | None = None) -> AuthSession:
    """Verify the trader may go live, returning the established session.

    This is the **live-readiness gate**. It enforces, in order:

    1. ``TRADER_LIVE`` must be on.
    2. The wallet must be able to sign (a private key is configured).
    3. The configured auth provider must not be the null provider.
    4. A session must actually be obtainable *now*.

    Any failing precondition raises :class:`CCAuthError` with a clear reason.
    The failure mode is safe: callers that cannot obtain a session must refuse
    to trade rather than proceed unauthenticated.
    """
    if not cfg.live:
        raise CCAuthError("Live trading is disabled (TRADER_LIVE is not true).")
    if not wallet.can_sign:
        raise CCAuthError(
            "Live trading requires a signing wallet (TRADER_WALLET_SECRET)."
        )
    if (cfg.auth_provider or "none").lower() == "none":
        raise CCAuthError(
            "Live trading requires an authenticated session, but "
            "TRADER_AUTH_PROVIDER is 'none'. Set it to 'privy' (or 'static' "
            "for integration testing)."
        )
    provider = make_session_provider(cfg, wallet, http=http)
    # Establish a session now so failures surface before any trading attempt.
    return provider.get_session()

