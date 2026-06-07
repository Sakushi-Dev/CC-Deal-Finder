"""Shared pytest fixtures, fakes and helpers for the live-mode test suite.

The goal of this suite is to prove the autonomous trader's **live mode** behaves
exactly as designed: it only ever acts when fully armed, it enforces every
preflight/risk guard, it never auto-retries a write (no double-spend), it never
leaks secrets, and uncertainty always resolves to *do not trade*.

Everything here is hermetic: no network, no real wallet key on disk, and every
test that touches the durable store uses an isolated temp database via the
``store``/``tmp_path`` fixtures. Real cryptographic signing is exercised with a
freshly generated in-memory Solana keypair (never a real funded wallet).
"""
from __future__ import annotations

import base64
from dataclasses import replace
from typing import Any

import pytest

from collectorcrypt.trader.config import TraderConfig
from collectorcrypt.trader.orders import (Order, OrderKind, OrderStatus,
                                          make_client_order_id)
from collectorcrypt.trader.store import OrderStore
from collectorcrypt.trader.wallet import Wallet, WalletError


# --------------------------------------------------------------------------- #
# base58 helper (solders exposes no base58 secret export; CC secrets are b58)
# --------------------------------------------------------------------------- #
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    """Minimal Bitcoin/Solana base58 encoder (used to build test wallet keys)."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    pad = 0
    for byte in raw:
        if byte == 0:
            pad += 1
        else:
            break
    return _B58_ALPHABET[0] * pad + out


def new_keypair():
    """Return a fresh in-memory solders Keypair (never a real funded wallet)."""
    from solders.keypair import Keypair

    return Keypair()


def keypair_secret(kp) -> str:
    """The base58-encoded 64-byte secret accepted by ``Wallet``."""
    return b58encode(bytes(kp))


def build_serialized_tx(kp, *, payer=None) -> str:
    """Build a tiny, valid, base64-encoded v0 transaction signable by ``kp``.

    Mirrors what the CC API hands back: a serialized transaction the wallet must
    decode, (re-)sign and re-encode. The payer defaults to ``kp``'s pubkey so a
    single-signer re-sign succeeds.
    """
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    payer_pk = payer or kp.pubkey()
    ix = Instruction(Pubkey.default(), b"test",
                     [AccountMeta(payer_pk, True, True)])
    msg = MessageV0.try_compile(payer_pk, [ix], [], Hash.default())
    tx = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(tx)).decode()


# --------------------------------------------------------------------------- #
# Config factory
# --------------------------------------------------------------------------- #
_CONFIG_DEFAULTS: dict[str, Any] = dict(
    rpc_url="https://rpc.test.invalid",
    wallet_address="",
    wallet_secret="",
    live=False,
    auth_provider="none",
    privy_app_id="",
    privy_client_id="",
    cc_token="",
    reserve_usdc=0.0,
    gas_reserve_sol=0.0,
    base_max_card_usd=40.0,
    min_card_usd=0.0,
    min_discount_pct=20.0,
    direct_buy_pct=50.0,
    offer_pct=50.0,
    offer_discount_pct=10.0,
    offer_max_premium_pct=0.0,
    resell_discount_pct=10.0,
    escalation_volume_usd=1000.0,
    escalation_max_card_usd=100.0,
    max_spend_per_cycle_usd=0.0,
    max_spend_per_day_usd=0.0,
    max_open_positions=0,
    max_consecutive_failures=0,
    offer_bump_usd=0.10,
    offer_bump_age_hours=24.0,
    offer_bump_max=3,
    min_operate_usd=0.0,
    max_owned_cards=0,
    unpopular_days=7.0,
    markdown_delay_days=3.0,
    markdown_step_pct=1.0,
    markdown_interval_days=3.0,
    offer_accept_delay_days=3.0,
    offer_accept_min_market_pct=0.0,
    market_recheck_hours=24.0,
    categories=("Pokemon",),
    max_pages=5,
    allowed_marketplaces=("CC",),
    loop_interval_sec=60.0,
    auto_resume=False,
)


def make_config(**overrides: Any) -> TraderConfig:
    """Construct a :class:`TraderConfig` with sensible test defaults."""
    data = {**_CONFIG_DEFAULTS, **overrides}
    return TraderConfig(**data)


@pytest.fixture
def cfg() -> TraderConfig:
    return make_config()


# --------------------------------------------------------------------------- #
# Fake wallet
# --------------------------------------------------------------------------- #
class FakeWallet:
    """A controllable stand-in for :class:`Wallet`.

    ``can_sign`` gates the live path. ``sign_transaction`` returns a deterministic
    string unless ``sign_error`` is set, in which case it raises
    :class:`WalletError` to exercise the signing-failure branch.
    """

    def __init__(self, *, can_sign: bool = True, address: str = "WALLEThdrtest",
                 sign_error: bool = False, sign_value: str = "SIGNED-TX") -> None:
        self.can_sign = can_sign
        self.address = address
        self._sign_error = sign_error
        self._sign_value = sign_value
        self.signed: list[str] = []
        self.signed_messages: list[bytes] = []

    def sign_transaction(self, serialized_tx: str) -> str:
        self.signed.append(serialized_tx)
        if self._sign_error:
            raise WalletError("signing failed (fake)")
        return self._sign_value

    def sign_message(self, message: bytes, *, encoding: str = "base58") -> str:
        self.signed_messages.append(message)
        self.last_message_encoding = encoding
        if self._sign_error:
            raise WalletError("message signing failed (fake)")
        return "FAKE-MSG-SIG"

    # balances (engine wiring only)
    def sol_balance(self) -> float:
        return 1.0

    def usdc_balance(self) -> float:
        return 1000.0

    def available_volume(self, reserve_usdc: float = 0.0) -> float:
        return max(0.0, 1000.0 - reserve_usdc)


@pytest.fixture
def fake_wallet() -> FakeWallet:
    return FakeWallet()


# --------------------------------------------------------------------------- #
# Fake trading client
# --------------------------------------------------------------------------- #
class FakeClient:
    """A scriptable CC trading client that records calls and never hits the net.

    Each write method returns a configurable response dict (default carries a
    transaction + external id so the executor proceeds to signing/broadcast).
    Set ``*_error`` to a callable/exception to make a method raise, or override
    the response via the ``responses`` mapping.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, BaseException] = {}
        # default broadcast resolves to confirmed/filled
        self.broadcast_response: Any = {"status": "confirmed",
                                        "signature": "SIG123"}

    # -- helpers ----------------------------------------------------------- #
    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def _maybe_raise(self, name: str) -> None:
        if name in self.errors:
            raise self.errors[name]

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    # -- writes ------------------------------------------------------------ #
    def initiate_buy(self, *, nft, price, wallet, currency="USDC",
                     funding_source="wallet", extra=None):
        self._record("initiate_buy", nft=nft, price=price, wallet=wallet,
                     currency=currency, funding_source=funding_source,
                     extra=extra)
        self._maybe_raise("initiate_buy")
        return self.responses.get(
            "initiate_buy",
            {"transaction": "UNSIGNED-BUY-TX", "receiptId": "rcpt-1"})

    def make_offer(self, *, nft, price, currency="USDC", extra=None):
        self._record("make_offer", nft=nft, price=price, currency=currency,
                     extra=extra)
        self._maybe_raise("make_offer")
        return self.responses.get(
            "make_offer",
            {"transaction": "UNSIGNED-OFFER-TX", "offerId": "offer-1"})

    def create_listing(self, *, nft, price, currency="USDC", extra=None):
        self._record("create_listing", nft=nft, price=price, currency=currency,
                     extra=extra)
        self._maybe_raise("create_listing")
        return self.responses.get(
            "create_listing",
            {"transaction": "UNSIGNED-LIST-TX", "listingId": "lst-1"})

    def broadcast(self, *, signed_tx, wallet="", nft="", extra=None):
        self._record("broadcast", signed_tx=signed_tx, wallet=wallet, nft=nft,
                     extra=extra)
        self._maybe_raise("broadcast")
        return self.broadcast_response

    def cancel_listing(self, *, nft="", listing_id=""):
        self._record("cancel_listing", nft=nft, listing_id=listing_id)
        self._maybe_raise("cancel_listing")
        return self.responses.get("cancel_listing", {"status": "ok"})

    def cancel_offer(self, *, offer_id):
        self._record("cancel_offer", offer_id=offer_id)
        self._maybe_raise("cancel_offer")
        return self.responses.get("cancel_offer", {"status": "ok"})

    # -- reads ------------------------------------------------------------- #
    def check_listing_status(self, *, nft, wallet):
        self._record("check_listing_status", nft=nft, wallet=wallet)
        self._maybe_raise("check_listing_status")
        return self.responses.get(
            "check_listing_status",
            {"exists": True, "marketplace": "CC", "listing": {}})

    def calc_listing_fee(self, *, nft, price, currency="USDC"):
        self._record("calc_listing_fee", nft=nft, price=price,
                     currency=currency)
        self._maybe_raise("calc_listing_fee")
        return self.responses.get("calc_listing_fee", {"fee": 0})


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


# --------------------------------------------------------------------------- #
# Fake session provider
# --------------------------------------------------------------------------- #
class FakeSessionProvider:
    """Records ``invalidate`` and returns/raises a configurable session."""

    def __init__(self, *, token: str = "tok", error: BaseException | None = None,
                 account_id: str = "acct") -> None:
        self._token = token
        self._error = error
        self._account_id = account_id
        self.invalidated = 0
        self.get_calls = 0

    def get_session(self):
        from collectorcrypt.trader.auth import AuthSession

        self.get_calls += 1
        if self._error is not None:
            raise self._error
        return AuthSession(token=self._token, account_id=self._account_id)

    def invalidate(self) -> None:
        self.invalidated += 1


# --------------------------------------------------------------------------- #
# Real store on an isolated temp DB
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path, monkeypatch) -> OrderStore:
    """A real :class:`OrderStore` backed by an isolated temp database."""
    db_path = tmp_path / "trader_store.db"
    monkeypatch.setenv("TRADER_STORE_PATH", str(db_path))
    return OrderStore(str(db_path))


# --------------------------------------------------------------------------- #
# Order / candidate builders
# --------------------------------------------------------------------------- #
def make_order(kind: OrderKind = OrderKind.BUY, *, nft: str = "NFT1",
               price_usd: float = 10.0, market_usd: float = 20.0,
               resell_usd: float = 0.0, cycle_id: str = "cyc1",
               simulated: bool = False, **kw: Any) -> Order:
    return Order(kind=kind, nft=nft, price_usd=price_usd, market_usd=market_usd,
                 resell_usd=resell_usd, cycle_id=cycle_id, simulated=simulated,
                 **kw)


def make_buy(**kw: Any) -> Order:
    return make_order(OrderKind.BUY, **kw)


def make_offer(**kw: Any) -> Order:
    return make_order(OrderKind.OFFER, **kw)


def make_list(**kw: Any) -> Order:
    kw.setdefault("market_usd", 20.0)
    return make_order(OrderKind.LIST, **kw)
