"""Authenticated session model for CollectorCrypt.

CollectorCrypt gates its trading endpoints (buy / make-offer / list / broadcast)
behind a Privy-issued bearer token obtained via a Sign-In-With-Solana (SIWS)
flow. This module models that **session state and its provider interface** so
the rest of the trader can depend on a stable abstraction instead of auth
internals. The concrete SIWS handshake is implemented in ETAPPE 4 as a
:class:`SessionProvider`; everything here is transport-agnostic and secret-free
on disk.

Design
------
* :class:`AuthSession` is an immutable snapshot of a live session — the bearer
  token, when it expires, and the resolved account id. It never knows *how* it
  was obtained.
* :class:`SessionProvider` is the seam: :meth:`get_session` returns a valid
  session (refreshing/establishing one as needed) or raises
  :class:`~collectorcrypt.trader.ccapi.CCAuthError`. The trading client only
  ever talks to this interface.
* Two ready providers ship here:
  - :class:`NullSessionProvider` — the safe default. It owns no credentials and
    always refuses, so an un-configured trader can never accidentally hit an
    authenticated endpoint.
  - :class:`StaticTokenProvider` — wraps a pre-obtained bearer token (e.g. from
    an env var during integration testing). It performs no handshake and is the
    minimal way to exercise the authenticated transport before ETAPPE 4 lands.

Security
--------
The bearer token is a secret. It is held only in memory, never written to the
order store, and redacted from all logs (see
:func:`collectorcrypt.trader.ccapi.redact`). Treat it like the wallet key.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol


# Refresh a session this many seconds *before* its real expiry, so a request is
# never sent with a token that lapses mid-flight.
EXPIRY_SKEW_SEC = 30.0


@dataclass(frozen=True)
class AuthSession:
    """An established, currently-usable authenticated session."""

    token: str
    account_id: str = ""
    wallet: str = ""
    expires_at: float = 0.0  # epoch seconds; 0 = unknown/non-expiring

    @property
    def is_valid(self) -> bool:
        """True if the token is present and not within the expiry skew window."""
        if not self.token:
            return False
        if self.expires_at <= 0:
            return True  # no known expiry -> treat as valid, provider decides
        return time.time() < (self.expires_at - EXPIRY_SKEW_SEC)

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class SessionProvider(Protocol):
    """Supplies valid :class:`AuthSession` objects to the trading client.

    Implementations must be **fail-safe**: if a valid session cannot be
    established they raise :class:`CCAuthError` rather than returning an
    invalid/empty session. Silent degradation is forbidden — the trader must
    refuse to act rather than send unauthenticated requests to a trading
    endpoint.
    """

    def get_session(self) -> AuthSession:
        """Return a valid session, establishing/refreshing as needed."""
        ...

    def invalidate(self) -> None:
        """Drop any cached session (e.g. after a 401) so the next call rebuilds."""
        ...


class NullSessionProvider:
    """The safe default: owns no credentials and always refuses.

    With this provider in place the trading client is technically wired but can
    never authenticate, guaranteeing no authenticated request is ever sent
    unless the operator explicitly configures a real provider (ETAPPE 4).
    """

    def get_session(self) -> AuthSession:
        from .ccapi import CCAuthError

        raise CCAuthError(
            "No authenticated CollectorCrypt session is configured. A "
            "SessionProvider (Privy SIWS, ETAPPE 4) must be supplied before "
            "any authenticated request can be made."
        )

    def invalidate(self) -> None:  # nothing to drop
        return None


class StaticTokenProvider:
    """Wraps a pre-obtained bearer token. Performs no handshake.

    Intended for integration testing of the authenticated transport before the
    full SIWS flow exists. The token is read from the constructor or, if not
    given, from the ``TRADER_CC_TOKEN`` environment variable (never from the
    UI-writable overrides file).
    """

    def __init__(self, token: str = "", *, account_id: str = "",
                 wallet: str = "", expires_at: float = 0.0) -> None:
        self._token = token or os.environ.get("TRADER_CC_TOKEN", "").strip()
        self._account_id = account_id
        self._wallet = wallet
        self._expires_at = expires_at

    def get_session(self) -> AuthSession:
        from .ccapi import CCAuthError

        if not self._token:
            raise CCAuthError(
                "StaticTokenProvider has no token (set TRADER_CC_TOKEN or pass "
                "one explicitly)."
            )
        session = AuthSession(
            token=self._token, account_id=self._account_id,
            wallet=self._wallet, expires_at=self._expires_at,
        )
        if not session.is_valid:
            raise CCAuthError("The configured CollectorCrypt token has expired.")
        return session

    def invalidate(self) -> None:
        # A static token cannot be refreshed; clearing it forces a clear error
        # on the next call instead of silently reusing a known-bad token.
        self._token = ""
