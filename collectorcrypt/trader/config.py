"""Trader configuration.

All values come from environment variables (see ``.env.example``). Secrets are
read once into a frozen :class:`TraderConfig`; nothing sensitive is hard-coded
or committed. ``.env`` is loaded automatically if ``python-dotenv`` is present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

try:  # Optional: load a local .env without making it a hard dependency.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is listed in requirements
    pass


# Solana mainnet USDC SPL mint.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# On-chain precision.
USDC_DECIMALS = 6
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 1_000_000_000

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"


def _get_str(src: Mapping[str, str], name: str, default: str = "") -> str:
    return (src.get(name) or default).strip()


def _get_float(src: Mapping[str, str], name: str, default: float) -> float:
    raw = src.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(src: Mapping[str, str], name: str, default: int) -> int:
    raw = src.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(src: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = src.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() == "true"


def _get_tuple(src: Mapping[str, str], name: str,
               default: tuple[str, ...]) -> tuple[str, ...]:
    raw = src.get(name)
    if raw is None or not str(raw).strip():
        return default
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


@dataclass(frozen=True)
class TraderConfig:
    """Immutable snapshot of the trader settings."""

    # Connectivity
    rpc_url: str
    wallet_address: str
    wallet_secret: str
    live: bool

    # Budget / volume
    reserve_usdc: float
    gas_reserve_sol: float

    # Quantity-first sizing
    base_max_card_usd: float
    min_discount_pct: float

    # Volume allocation (split of the available volume)
    direct_buy_pct: float
    offer_pct: float
    offer_discount_pct: float
    offer_max_premium_pct: float

    # Resale (relisting bought cards for a profit)
    resell_discount_pct: float

    # Escalation protocol
    escalation_volume_usd: float
    escalation_max_card_usd: float

    # Sourcing
    categories: tuple[str, ...]
    max_pages: int
    allowed_marketplaces: tuple[str, ...]

    # Loop
    loop_interval_sec: float

    @property
    def has_secret(self) -> bool:
        return bool(self.wallet_secret)


def load_config() -> TraderConfig:
    """Build a :class:`TraderConfig` from env, layered with UI overrides.

    Resolution order (highest priority last): process environment / ``.env``
    first, then the local ``trader_settings.json`` overrides written by the
    trader UI. Secrets (``TRADER_WALLET_SECRET``) are intentionally only read
    from the environment and never from the overrides file.
    """
    from .settings import load_overrides

    overrides = load_overrides()
    # Never let a secret come from the (UI-written) overrides file.
    overrides.pop("TRADER_WALLET_SECRET", None)
    src: dict[str, str] = {**os.environ, **overrides}

    return TraderConfig(
        rpc_url=_get_str(src, "TRADER_RPC_URL", DEFAULT_RPC_URL),
        wallet_address=_get_str(src, "TRADER_WALLET_ADDRESS"),
        wallet_secret=_get_str(os.environ, "TRADER_WALLET_SECRET"),
        live=_get_bool(os.environ, "TRADER_LIVE", False),
        reserve_usdc=_get_float(src, "TRADER_RESERVE_USDC", 0.0),
        gas_reserve_sol=_get_float(src, "TRADER_GAS_RESERVE_SOL", 0.05),
        base_max_card_usd=_get_float(src, "TRADER_BASE_MAX_CARD_USD", 100.0),
        min_discount_pct=_get_float(src, "TRADER_MIN_DISCOUNT_PCT", 0.0),
        direct_buy_pct=_get_float(src, "TRADER_DIRECT_BUY_PCT", 100.0),
        offer_pct=_get_float(src, "TRADER_OFFER_PCT", 0.0),
        offer_discount_pct=_get_float(src, "TRADER_OFFER_DISCOUNT_PCT", 10.0),
        offer_max_premium_pct=_get_float(src, "TRADER_OFFER_MAX_PREMIUM_PCT", 10.0),
        resell_discount_pct=_get_float(src, "TRADER_RESELL_DISCOUNT_PCT", 10.0),
        escalation_volume_usd=_get_float(src, "TRADER_ESCALATION_VOLUME_USD", 1000.0),
        escalation_max_card_usd=_get_float(src, "TRADER_ESCALATION_MAX_CARD_USD", 1000.0),
        categories=_get_tuple(src, "TRADER_CATEGORIES", ("",)),
        max_pages=_get_int(src, "TRADER_MAX_PAGES", 10),
        allowed_marketplaces=_get_tuple(src, "TRADER_ALLOWED_MARKETPLACES", ("CC",)),
        loop_interval_sec=_get_float(src, "TRADER_LOOP_INTERVAL_SEC", 300.0),
    )
