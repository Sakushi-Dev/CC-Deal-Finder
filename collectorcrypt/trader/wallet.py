"""Solana wallet access for the trader.

Read-only balance queries go straight to the JSON-RPC endpoint via ``requests``
(no heavy SDK needed to read SOL / USDC). The private key is only ever touched
lazily — when an actual signing keypair is requested for live execution — so a
pure dry-run never imports ``solders`` and never needs the secret.
"""
from __future__ import annotations

from typing import Any

import requests

from . import config


class WalletError(RuntimeError):
    """Raised for RPC failures or missing/invalid wallet credentials."""


class Wallet:
    """A Solana wallet the bot can read (and, in live mode, sign for).

    Provide either ``address`` (read-only) or ``secret`` (base58 private key,
    enables signing and also derives the address). If both are given they must
    match.
    """

    def __init__(self, rpc_url: str, *, address: str = "", secret: str = "",
                 timeout: float = 30.0) -> None:
        self._rpc_url = rpc_url
        self._secret = secret
        self._timeout = timeout
        self._session = requests.Session()
        self._keypair: Any | None = None  # lazily built solders Keypair

        derived = self._derive_address(secret) if secret else ""
        if address and derived and address != derived:
            raise WalletError(
                "TRADER_WALLET_ADDRESS does not match the address derived "
                "from TRADER_WALLET_SECRET."
            )
        self._address = address or derived
        if not self._address:
            raise WalletError(
                "No wallet configured: set TRADER_WALLET_ADDRESS (dry-run) or "
                "TRADER_WALLET_SECRET (live)."
            )

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    @property
    def address(self) -> str:
        return self._address

    @property
    def can_sign(self) -> bool:
        return bool(self._secret)

    def keypair(self) -> Any:
        """Return the signing keypair (live mode only). Imports ``solders``
        lazily so dry-runs never need it installed."""
        if not self._secret:
            raise WalletError("No private key configured; cannot sign.")
        if self._keypair is None:
            self._keypair = self._load_keypair(self._secret)
        return self._keypair

    def sign_message(self, message: bytes) -> str:
        """Sign an arbitrary message and return the base58 signature.

        Used by the Sign-In-With-Solana (SIWS) auth flow to prove wallet
        ownership to CollectorCrypt/Privy. The private key never leaves this
        process; only the resulting signature is transmitted. Requires a
        configured secret (live mode); a read-only wallet cannot sign.
        """
        keypair = self.keypair()
        try:
            signature = keypair.sign_message(message)
        except Exception as exc:  # noqa: BLE001 - surface signing failures clearly
            raise WalletError(f"Failed to sign message: {exc}") from exc
        return str(signature)

    def sign_transaction(self, serialized_tx: str) -> str:
        """Sign a serialized Solana transaction and return the signed tx.

        The CollectorCrypt buy/offer/list endpoints return an *unsigned*
        transaction that the buyer must sign locally and hand back to
        ``marketplace/broadcast``. This decodes that transaction, applies this
        wallet's signature over the existing message and re-encodes it.

        The private key never leaves this process; only the fully signed
        transaction bytes are returned for broadcast. Requires a configured
        secret (live mode).

        ASSUMPTION (unverified — see docs/api.md): the wire encoding is base64
        and the payload is a *versioned* (v0) transaction whose only required
        signer at this stage is this wallet. The decode falls back to a legacy
        transaction layout. The exact encoding/version must be confirmed against
        the live API on a funded test wallet before this drives real spend; the
        method raises clearly rather than guessing if it cannot parse the input.
        """
        import base64

        keypair = self.keypair()
        raw = serialized_tx.strip()
        try:
            tx_bytes = base64.b64decode(raw, validate=True)
        except (ValueError, Exception) as exc:  # noqa: BLE001
            raise WalletError(
                "Could not base64-decode the transaction payload returned by "
                "CollectorCrypt. The wire encoding is an unverified assumption; "
                "confirm it before enabling live trading."
            ) from exc

        signed = self._sign_tx_bytes(tx_bytes, keypair)
        return base64.b64encode(signed).decode("ascii")

    @staticmethod
    def _sign_tx_bytes(tx_bytes: bytes, keypair: Any) -> bytes:
        """Re-sign a serialized transaction with ``keypair``; return its bytes.

        Tries the versioned (v0) layout first, then the legacy layout. Signing
        rebuilds the transaction from its message with this keypair as signer,
        which is correct when the wallet is the sole required signer (the usual
        case for a marketplace buy/offer). Any failure is surfaced as a
        :class:`WalletError` so live execution refuses rather than broadcasting
        a malformed transaction.
        """
        try:
            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed = VersionedTransaction(tx.message, [keypair])
            return bytes(signed)
        except Exception as versioned_exc:  # noqa: BLE001 - try legacy next
            try:
                from solders.transaction import Transaction
                tx = Transaction.from_bytes(tx_bytes)
                tx.sign([keypair], tx.message.recent_blockhash)
                return bytes(tx)
            except Exception as legacy_exc:  # noqa: BLE001
                raise WalletError(
                    "Failed to sign transaction (versioned: "
                    f"{versioned_exc}; legacy: {legacy_exc}). The transaction "
                    "format is an unverified assumption."
                ) from legacy_exc

    # ------------------------------------------------------------------ #
    # Balances
    # ------------------------------------------------------------------ #
    def sol_balance(self) -> float:
        """Native SOL balance in SOL."""
        result = self._rpc("getBalance", [self._address])
        lamports = int((result or {}).get("value") or 0)
        return lamports / config.LAMPORTS_PER_SOL

    def usdc_balance(self) -> float:
        """USDC balance (sum across token accounts for the USDC mint)."""
        result = self._rpc(
            "getTokenAccountsByOwner",
            [
                self._address,
                {"mint": config.USDC_MINT},
                {"encoding": "jsonParsed"},
            ],
        )
        total = 0.0
        for entry in (result or {}).get("value", []) or []:
            try:
                info = entry["account"]["data"]["parsed"]["info"]
                amount = info["tokenAmount"]["uiAmount"]
            except (KeyError, TypeError):
                continue
            if amount:
                total += float(amount)
        return total

    def available_volume(self, reserve_usdc: float) -> float:
        """Spendable USDC = balance minus the untouchable reserve (>= 0)."""
        return max(0.0, self.usdc_balance() - max(0.0, reserve_usdc))

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _rpc(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            r = self._session.post(self._rpc_url, json=payload, timeout=self._timeout)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise WalletError(f"RPC {method} failed: {exc}") from exc
        if "error" in data:
            raise WalletError(f"RPC {method} error: {data['error']}")
        return data.get("result")

    @staticmethod
    def _derive_address(secret: str) -> str:
        return str(Wallet._load_keypair(secret).pubkey())

    @staticmethod
    def _load_keypair(secret: str) -> Any:
        try:
            from solders.keypair import Keypair
        except ImportError as exc:  # pragma: no cover
            raise WalletError(
                "solders is required to use a private key. Install it with "
                "'pip install solders'."
            ) from exc
        try:
            return Keypair.from_base58_string(secret)
        except (ValueError, Exception) as exc:  # noqa: BLE001 - surface clearly
            raise WalletError(f"Invalid wallet secret: {exc}") from exc
