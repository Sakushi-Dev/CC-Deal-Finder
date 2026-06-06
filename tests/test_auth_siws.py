"""Authentication session + Privy SIWS provider tests.

The session layer is the gate that keeps the trader from ever sending an
unauthenticated request to a trading endpoint. The providers must be
**fail-safe**: any deviation (no token, expired, HTTP error) raises rather than
returning an invalid session.
"""
from __future__ import annotations

import time

import pytest

from collectorcrypt.trader import siws
from collectorcrypt.trader.auth import (AuthSession, NullSessionProvider,
                                        StaticTokenProvider)
from collectorcrypt.trader.ccapi import CCAuthError, CCNetworkError
from collectorcrypt.trader.siws import (PrivySiwsProvider, check_live_ready,
                                        make_session_provider)

from .conftest import FakeWallet, make_config


# --------------------------------------------------------------------------- #
# AuthSession
# --------------------------------------------------------------------------- #
def test_auth_header_format():
    s = AuthSession(token="abc")
    assert s.auth_header() == {"Authorization": "Bearer abc"}


def test_session_valid_without_expiry():
    assert AuthSession(token="abc").is_valid


def test_session_invalid_without_token():
    assert not AuthSession(token="").is_valid


def test_session_valid_future_expiry():
    assert AuthSession(token="abc", expires_at=time.time() + 3600).is_valid


def test_session_invalid_past_expiry():
    assert not AuthSession(token="abc", expires_at=time.time() - 10).is_valid


def test_session_invalid_within_skew():
    # Within the 30s skew window it must already be considered invalid.
    assert not AuthSession(token="abc", expires_at=time.time() + 5).is_valid


# --------------------------------------------------------------------------- #
# NullSessionProvider
# --------------------------------------------------------------------------- #
def test_null_provider_always_refuses():
    with pytest.raises(CCAuthError):
        NullSessionProvider().get_session()


def test_null_provider_invalidate_noop():
    assert NullSessionProvider().invalidate() is None


# --------------------------------------------------------------------------- #
# StaticTokenProvider
# --------------------------------------------------------------------------- #
def test_static_provider_returns_session():
    p = StaticTokenProvider("tok123")
    assert p.get_session().token == "tok123"


def test_static_provider_reads_env(monkeypatch):
    monkeypatch.setenv("TRADER_CC_TOKEN", "envtok")
    assert StaticTokenProvider().get_session().token == "envtok"


def test_static_provider_no_token_raises(monkeypatch):
    monkeypatch.delenv("TRADER_CC_TOKEN", raising=False)
    with pytest.raises(CCAuthError):
        StaticTokenProvider().get_session()


def test_static_provider_expired_raises():
    with pytest.raises(CCAuthError):
        StaticTokenProvider("tok", expires_at=time.time() - 10).get_session()


def test_static_provider_invalidate_forces_error():
    p = StaticTokenProvider("tok")
    p.get_session()
    p.invalidate()
    with pytest.raises(CCAuthError):
        p.get_session()


def test_static_provider_carries_account_id():
    p = StaticTokenProvider("tok", account_id="acct9")
    assert p.get_session().account_id == "acct9"


# --------------------------------------------------------------------------- #
# Response parsing helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["token", "access_token", "accessToken", "jwt",
                                 "privy_access_token", "session_token"])
def test_extract_token_keys(key):
    assert siws._extract_token({key: "TOK"}) == "TOK"


def test_extract_token_nested_session():
    assert siws._extract_token({"session": {"token": "TOK"}}) == "TOK"


def test_extract_token_missing():
    assert siws._extract_token({"foo": "bar"}) == ""


@pytest.mark.parametrize("key", ["expires_at", "expiresAt", "exp"])
def test_extract_expiry_absolute(key):
    ts = time.time() + 1000
    assert siws._extract_expiry({key: ts}) == pytest.approx(ts)


@pytest.mark.parametrize("key", ["expires_in", "expiresIn", "ttl"])
def test_extract_expiry_relative(key):
    out = siws._extract_expiry({key: 100})
    assert out == pytest.approx(time.time() + 100, abs=5)


def test_extract_expiry_default_ttl():
    out = siws._extract_expiry({})
    assert out == pytest.approx(time.time() + siws.DEFAULT_SESSION_TTL_SEC, abs=5)


# --------------------------------------------------------------------------- #
# make_session_provider factory
# --------------------------------------------------------------------------- #
def test_factory_none_returns_null():
    cfg = make_config(auth_provider="none")
    p = make_session_provider(cfg, FakeWallet())
    assert isinstance(p, NullSessionProvider)


def test_factory_static_returns_static():
    cfg = make_config(auth_provider="static", cc_token="tok")
    p = make_session_provider(cfg, FakeWallet())
    assert isinstance(p, StaticTokenProvider)
    assert p.get_session().token == "tok"


def test_factory_privy_returns_privy():
    cfg = make_config(auth_provider="privy")
    p = make_session_provider(cfg, FakeWallet(can_sign=True))
    assert isinstance(p, PrivySiwsProvider)


def test_factory_unknown_returns_null():
    cfg = make_config(auth_provider="weird")
    assert isinstance(make_session_provider(cfg, FakeWallet()),
                      NullSessionProvider)


def test_factory_privy_requires_signing_wallet():
    cfg = make_config(auth_provider="privy")
    with pytest.raises(CCAuthError):
        make_session_provider(cfg, FakeWallet(can_sign=False))


# --------------------------------------------------------------------------- #
# check_live_ready gate
# --------------------------------------------------------------------------- #
def test_live_ready_rejects_when_not_live():
    cfg = make_config(live=False, auth_provider="static", cc_token="tok")
    with pytest.raises(CCAuthError):
        check_live_ready(cfg, FakeWallet(can_sign=True))


def test_live_ready_rejects_without_signing():
    cfg = make_config(live=True, auth_provider="static", cc_token="tok")
    with pytest.raises(CCAuthError):
        check_live_ready(cfg, FakeWallet(can_sign=False))


def test_live_ready_rejects_auth_none():
    cfg = make_config(live=True, auth_provider="none")
    with pytest.raises(CCAuthError):
        check_live_ready(cfg, FakeWallet(can_sign=True))


def test_live_ready_returns_session_static():
    cfg = make_config(live=True, auth_provider="static", cc_token="tok")
    session = check_live_ready(cfg, FakeWallet(can_sign=True))
    assert session.token == "tok"


# --------------------------------------------------------------------------- #
# PrivySiwsProvider handshake (fake HTTP)
# --------------------------------------------------------------------------- #
class FakeSiwsResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSiwsHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_privy(responses, *, app_id=""):
    http = FakeSiwsHTTP(responses)
    wallet = FakeWallet(can_sign=True, address="WALLETADDR")
    provider = PrivySiwsProvider(wallet, app_id=app_id, http=http)
    return provider, http, wallet


def test_privy_full_handshake_returns_session():
    provider, http, _ = make_privy([
        FakeSiwsResp(200, {"nonce": "abc123"}),
        FakeSiwsResp(200, {"token": "bearer-tok", "expires_in": 3600,
                           "accountId": "acct7"}),
    ])
    session = provider.get_session()
    assert session.token == "bearer-tok"
    assert session.account_id == "acct7"
    assert session.wallet == "WALLETADDR"
    assert len(http.calls) == 2


def test_privy_signs_the_challenge():
    provider, _, wallet = make_privy([
        FakeSiwsResp(200, {"nonce": "abc123"}),
        FakeSiwsResp(200, {"token": "tok"}),
    ])
    provider.get_session()
    assert len(wallet.signed_messages) == 1


def test_privy_caches_session():
    provider, http, _ = make_privy([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(200, {"token": "tok", "expires_in": 3600}),
    ])
    provider.get_session()
    provider.get_session()  # cached, no new HTTP
    assert len(http.calls) == 2


def test_privy_reauth_after_invalidate():
    provider, http, _ = make_privy([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(200, {"token": "tok", "expires_in": 3600}),
        FakeSiwsResp(200, {"nonce": "n2"}),
        FakeSiwsResp(200, {"token": "tok2", "expires_in": 3600}),
    ])
    provider.get_session()
    provider.invalidate()
    s2 = provider.get_session()
    assert s2.token == "tok2"
    assert len(http.calls) == 4


def test_privy_no_token_raises():
    provider, _, _ = make_privy([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(200, {"somethingelse": "x"}),
    ])
    with pytest.raises(CCAuthError):
        provider.get_session()


def test_privy_401_raises_auth_error():
    provider, _, _ = make_privy([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(401, {"message": "denied"}),
    ])
    with pytest.raises(CCAuthError):
        provider.get_session()


def test_privy_network_error_raises():
    import requests

    provider, _, _ = make_privy([requests.ConnectionError("down")])
    with pytest.raises(CCNetworkError):
        provider.get_session()


def test_privy_app_id_header_sent():
    provider, http, _ = make_privy([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(200, {"token": "tok"}),
    ], app_id="app-77")
    provider.get_session()
    assert http.calls[0]["headers"]["privy-app-id"] == "app-77"


def test_privy_authenticate_body_verified_shape():
    # Verified body (2026-06-06): message + base64 signature + connector fields,
    # NO address/nonce keys, walletClientType capitalised.
    provider, http, _ = make_privy([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(200, {"token": "tok"}),
    ])
    provider.get_session()
    auth_body = http.calls[1]["json"]
    assert set(auth_body) == {
        "message", "signature", "walletClientType",
        "connectorType", "mode", "message_type",
    }
    assert auth_body["walletClientType"] == "Phantom"
    assert auth_body["connectorType"] == "solana_adapter"
    assert auth_body["mode"] == "login-or-sign-up"
    assert auth_body["message_type"] == "plain"
    assert "address" not in auth_body and "nonce" not in auth_body


def test_privy_signature_is_base64_encoded():
    provider, _, wallet = make_privy([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(200, {"token": "tok"}),
    ])
    provider.get_session()
    assert wallet.last_message_encoding == "base64"


def test_privy_sends_origin_and_client_headers():
    http = FakeSiwsHTTP([
        FakeSiwsResp(200, {"nonce": "n"}),
        FakeSiwsResp(200, {"token": "tok"}),
    ])
    wallet = FakeWallet(can_sign=True, address="WALLETADDR")
    provider = PrivySiwsProvider(wallet, app_id="app-77",
                                 client_id="client-9", http=http)
    provider.get_session()
    headers = http.calls[1]["headers"]
    assert headers["Origin"] == "https://collectorcrypt.com"
    assert headers["privy-client-id"] == "client-9"
    assert headers["privy-ca-id"]  # a per-instance uuid
    assert headers["privy-client"].startswith("react-auth:")


def test_privy_message_template_matches_verified():
    provider, _, wallet = make_privy([
        FakeSiwsResp(200, {"nonce": "NONCE123"}),
        FakeSiwsResp(200, {"token": "tok"}),
    ])
    provider.get_session()
    msg = wallet.signed_messages[0].decode("utf-8")
    assert msg.startswith(
        "collectorcrypt.com wants you to sign in with your Solana account:\n"
        "WALLETADDR\n\nYou are proving you own WALLETADDR.\n\n")
    assert "Chain ID: mainnet\n" in msg
    assert "Nonce: NONCE123\n" in msg
    assert msg.endswith("Resources:\n- https://privy.io")



def test_privy_uses_prebuilt_message():
    provider, _, wallet = make_privy([
        FakeSiwsResp(200, {"nonce": "n", "message": "SIGN THIS EXACT"}),
        FakeSiwsResp(200, {"token": "tok"}),
    ])
    provider.get_session()
    assert wallet.signed_messages[0] == b"SIGN THIS EXACT"


def test_privy_signing_failure_raises():
    http = FakeSiwsHTTP([FakeSiwsResp(200, {"nonce": "n"})])
    wallet = FakeWallet(can_sign=True, sign_error=True)
    provider = PrivySiwsProvider(wallet, http=http)
    with pytest.raises(CCAuthError):
        provider.get_session()
