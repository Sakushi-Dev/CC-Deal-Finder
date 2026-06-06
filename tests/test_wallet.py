"""Wallet tests — identity, real cryptographic signing, and RPC parsing.

Signing is the irreversible heart of live mode: a malformed signature, a leaked
key, or a silent failure here is catastrophic. These tests exercise the *real*
solders signing path with a freshly generated in-memory keypair (never a funded
wallet) and mock only the JSON-RPC HTTP boundary.
"""
from __future__ import annotations

import base64

import pytest

from collectorcrypt.trader import config as app_config
from collectorcrypt.trader.wallet import Wallet, WalletError

from .conftest import build_serialized_tx, keypair_secret, new_keypair


@pytest.fixture
def kp():
    return new_keypair()


@pytest.fixture
def secret(kp):
    return keypair_secret(kp)


@pytest.fixture
def signer(secret):
    return Wallet("https://rpc.test.invalid", secret=secret)


# --------------------------------------------------------------------------- #
# Identity / construction
# --------------------------------------------------------------------------- #
def test_secret_enables_signing(signer):
    assert signer.can_sign is True


def test_address_derived_from_secret(kp, signer):
    assert signer.address == str(kp.pubkey())


def test_read_only_wallet_cannot_sign():
    w = Wallet("https://rpc.test.invalid", address="SomeAddr1111")
    assert w.can_sign is False


def test_address_and_secret_match_ok(kp):
    addr = str(kp.pubkey())
    w = Wallet("https://rpc.test.invalid", address=addr,
               secret=keypair_secret(kp))
    assert w.address == addr


def test_address_secret_mismatch_raises(kp):
    other = new_keypair()
    with pytest.raises(WalletError):
        Wallet("https://rpc.test.invalid", address=str(other.pubkey()),
               secret=keypair_secret(kp))


def test_no_credentials_raises():
    with pytest.raises(WalletError):
        Wallet("https://rpc.test.invalid")


def test_invalid_secret_raises():
    with pytest.raises(WalletError):
        Wallet("https://rpc.test.invalid", secret="not-a-valid-base58-secret!!")


def test_keypair_requires_secret():
    w = Wallet("https://rpc.test.invalid", address="Addr1111")
    with pytest.raises(WalletError):
        w.keypair()


def test_keypair_is_cached(signer):
    assert signer.keypair() is signer.keypair()


# --------------------------------------------------------------------------- #
# Transaction signing (real solders round-trip)
# --------------------------------------------------------------------------- #
def test_sign_transaction_returns_base64(kp, signer):
    tx = build_serialized_tx(kp)
    signed = signer.sign_transaction(tx)
    # Output must be valid base64 of a non-trivial transaction.
    raw = base64.b64decode(signed, validate=True)
    assert len(raw) > 0


def test_signed_transaction_has_signature(kp, signer):
    from solders.transaction import VersionedTransaction

    signed = signer.sign_transaction(build_serialized_tx(kp))
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
    assert len(tx.signatures) == 1
    # The signature must no longer be the default (all-zero) placeholder.
    from solders.signature import Signature
    assert tx.signatures[0] != Signature.default()


def test_sign_transaction_is_deterministic(kp, signer):
    tx = build_serialized_tx(kp)
    assert signer.sign_transaction(tx) == signer.sign_transaction(tx)


def test_sign_transaction_tolerates_whitespace(kp, signer):
    tx = build_serialized_tx(kp)
    assert signer.sign_transaction("  " + tx + "\n") == signer.sign_transaction(tx)


@pytest.mark.parametrize("bad", ["", "@@@@", "not base64!", "zzz==="])
def test_sign_transaction_invalid_base64_raises(signer, bad):
    with pytest.raises(WalletError):
        signer.sign_transaction(bad)


def test_sign_transaction_undecodable_payload_raises(signer):
    # Valid base64 but not a parseable transaction.
    garbage = base64.b64encode(b"\x01\x02\x03\x04").decode()
    with pytest.raises(WalletError):
        signer.sign_transaction(garbage)


def test_sign_transaction_requires_secret():
    w = Wallet("https://rpc.test.invalid", address="Addr1111")
    with pytest.raises(WalletError):
        w.sign_transaction(base64.b64encode(b"x").decode())


# --------------------------------------------------------------------------- #
# Message signing (SIWS)
# --------------------------------------------------------------------------- #
def test_sign_message_returns_base58(signer):
    sig = signer.sign_message(b"hello collectorcrypt")
    assert isinstance(sig, str) and len(sig) > 0


def test_sign_message_varies_with_input(signer):
    assert signer.sign_message(b"a") != signer.sign_message(b"b")


def test_sign_message_requires_secret():
    w = Wallet("https://rpc.test.invalid", address="Addr1111")
    with pytest.raises(WalletError):
        w.sign_message(b"x")


# --------------------------------------------------------------------------- #
# Balance RPC parsing (mocked HTTP)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def test_sol_balance_parses_lamports(signer, monkeypatch):
    monkeypatch.setattr(signer._session, "post",
                        lambda *a, **k: _Resp({"result": {"value": app_config.LAMPORTS_PER_SOL}}))
    assert signer.sol_balance() == 1.0


def test_sol_balance_zero_when_missing(signer, monkeypatch):
    monkeypatch.setattr(signer._session, "post",
                        lambda *a, **k: _Resp({"result": {"value": 0}}))
    assert signer.sol_balance() == 0.0


def test_usdc_balance_sums_accounts(signer, monkeypatch):
    payload = {"result": {"value": [
        {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 100.0}}}}}},
        {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 50.0}}}}}},
    ]}}
    monkeypatch.setattr(signer._session, "post", lambda *a, **k: _Resp(payload))
    assert signer.usdc_balance() == 150.0


def test_usdc_balance_skips_malformed_entries(signer, monkeypatch):
    payload = {"result": {"value": [
        {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 25.0}}}}}},
        {"account": {"data": {}}},
    ]}}
    monkeypatch.setattr(signer._session, "post", lambda *a, **k: _Resp(payload))
    assert signer.usdc_balance() == 25.0


def test_available_volume_subtracts_reserve(signer, monkeypatch):
    payload = {"result": {"value": [
        {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 100.0}}}}}},
    ]}}
    monkeypatch.setattr(signer._session, "post", lambda *a, **k: _Resp(payload))
    assert signer.available_volume(30.0) == 70.0


def test_available_volume_never_negative(signer, monkeypatch):
    payload = {"result": {"value": [
        {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 10.0}}}}}},
    ]}}
    monkeypatch.setattr(signer._session, "post", lambda *a, **k: _Resp(payload))
    assert signer.available_volume(50.0) == 0.0


def test_rpc_error_field_raises(signer, monkeypatch):
    monkeypatch.setattr(signer._session, "post",
                        lambda *a, **k: _Resp({"error": {"message": "boom"}}))
    with pytest.raises(WalletError):
        signer.sol_balance()


def test_rpc_network_error_raises(signer, monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(signer._session, "post", _boom)
    with pytest.raises(WalletError):
        signer.sol_balance()


def test_rpc_http_error_raises(signer, monkeypatch):
    monkeypatch.setattr(signer._session, "post",
                        lambda *a, **k: _Resp({}, status=500))
    with pytest.raises(WalletError):
        signer.sol_balance()
