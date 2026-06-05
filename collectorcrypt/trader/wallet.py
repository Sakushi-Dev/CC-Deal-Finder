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
