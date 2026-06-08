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

# Verified public Privy app identifiers for CollectorCrypt (from the frontend
# bundle / a captured live request, 2026-06-06). These are NOT secrets — they
# are sent in plain request headers by every browser client. Used as defaults
# so the SIWS provider works without extra env wiring; still overridable.
DEFAULT_PRIVY_APP_ID = "cmdgt21w400lgky0mkn069jui"
DEFAULT_PRIVY_CLIENT_ID = "client-WY6NvtFJDWADQMppqbxv6hSrGa1igpPo8eVK9DfhnSGTi"


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

    # Authentication (env-only; never UI-editable)
    auth_provider: str        # "none" | "static" | "privy"
    privy_app_id: str
    privy_client_id: str
    cc_token: str

    # Budget / volume
    reserve_usdc: float
    gas_reserve_sol: float

    # Quantity-first sizing
    base_max_card_usd: float
    min_card_usd: float
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

    # Risk limits (ETAPPE 7). 0 = disabled (no cap) so existing setups are
    # unaffected until an operator opts into a limit. Enforced on live cycles.
    max_spend_per_cycle_usd: float
    max_spend_per_day_usd: float
    max_open_positions: int
    max_consecutive_failures: int

    # Offer penetration (holdings-lifecycle). An aged open offer is bumped a
    # small amount to re-trigger the owner's notification, up to a max count,
    # then cancelled (escrow refunds).
    offer_bump_usd: float
    offer_bump_age_hours: float
    offer_bump_max: int

    # Holdings lifecycle (holdings-lifecycle). 0 = disabled where it is a cap or
    # threshold, so existing setups are unchanged until an operator opts in.
    min_operate_usd: float        # below this available volume, stop acquiring
    max_owned_cards: int          # cap on actually held (unsold) cards
    unpopular_days: float         # held + unsold this long -> blacklist
    markdown_delay_days: float    # unsold this long -> start the markdown curve
    markdown_step_pct: float      # markdown step, % of market value at buy
    markdown_interval_days: float # days between markdown steps
    offer_accept_delay_days: float  # days after floor -> accept best offer
    offer_accept_min_market_pct: float  # min market % to accept an incoming bid
    market_recheck_hours: float   # held-card market re-check interval

    # Sourcing
    categories: tuple[str, ...]
    max_pages: int
    allowed_marketplaces: tuple[str, ...]

    # Loop
    loop_interval_sec: float

    # Operational records (audit trail). The transaction ledger is an
    # append-only CSV of every real money event (provable trade history); the
    # bot log is a human-readable activity log. Empty disables that record.
    ledger_path: str
    log_path: str

    # Resilience (ETAPPE 8). When true, a running auto-loop is resumed after an
    # application restart/crash (env-only, like the live switch, so a crash can
    # never silently start trading the operator did not configure).
    auto_resume: bool

    @property
    def has_secret(self) -> bool:
        return bool(self.wallet_secret)

    @property
    def requires_auth(self) -> bool:
        """True when live trading is requested and must hold a real session.

        Live trading against CollectorCrypt is impossible without an
        authenticated session, so when ``live`` is on the trader must use a
        non-null auth provider. This drives the live-readiness gate.
        """
        return self.live


def load_config() -> TraderConfig:
    """Build a :class:`TraderConfig` from env, layered with UI overrides.

    Strategy tuning resolves with the local ``trader_settings.json`` (written by
    the UI; template ``trader_settings.example.json``) taking priority over any
    leftover environment / ``.env`` value. Security and connection variables —
    the wallet secret, ``TRADER_LIVE``, the auth provider/credentials,
    ``TRADER_AUTO_RESUME`` and the connection (``TRADER_RPC_URL``,
    ``TRADER_WALLET_ADDRESS``) — are read from the **environment only** and never
    from the overrides file, so they cannot be changed via the UI.
    """
    from .settings import load_overrides

    overrides = load_overrides()
    # Never let a secret come from the (UI-written) overrides file.
    overrides.pop("TRADER_WALLET_SECRET", None)
    src: dict[str, str] = {**os.environ, **overrides}

    return TraderConfig(
        rpc_url=_get_str(os.environ, "TRADER_RPC_URL", DEFAULT_RPC_URL),
        wallet_address=_get_str(os.environ, "TRADER_WALLET_ADDRESS"),
        wallet_secret=_get_str(os.environ, "TRADER_WALLET_SECRET"),
        live=_get_bool(os.environ, "TRADER_LIVE", False),
        auth_provider=_get_str(os.environ, "TRADER_AUTH_PROVIDER", "none").lower(),
        privy_app_id=_get_str(os.environ, "TRADER_PRIVY_APP_ID", DEFAULT_PRIVY_APP_ID),
        privy_client_id=_get_str(os.environ, "TRADER_PRIVY_CLIENT_ID", DEFAULT_PRIVY_CLIENT_ID),
        cc_token=_get_str(os.environ, "TRADER_CC_TOKEN"),
        reserve_usdc=_get_float(src, "TRADER_RESERVE_USDC", 0.0),
        gas_reserve_sol=_get_float(src, "TRADER_GAS_RESERVE_SOL", 0.05),
        base_max_card_usd=_get_float(src, "TRADER_BASE_MAX_CARD_USD", 100.0),
        min_card_usd=_get_float(src, "TRADER_MIN_CARD_USD", 0.0),
        min_discount_pct=_get_float(src, "TRADER_MIN_DISCOUNT_PCT", 0.0),
        direct_buy_pct=_get_float(src, "TRADER_DIRECT_BUY_PCT", 100.0),
        offer_pct=_get_float(src, "TRADER_OFFER_PCT", 0.0),
        offer_discount_pct=_get_float(src, "TRADER_OFFER_DISCOUNT_PCT", 10.0),
        offer_max_premium_pct=_get_float(src, "TRADER_OFFER_MAX_PREMIUM_PCT", 10.0),
        resell_discount_pct=_get_float(src, "TRADER_RESELL_DISCOUNT_PCT", 10.0),
        escalation_volume_usd=_get_float(src, "TRADER_ESCALATION_VOLUME_USD", 1000.0),
        escalation_max_card_usd=_get_float(src, "TRADER_ESCALATION_MAX_CARD_USD", 1000.0),
        max_spend_per_cycle_usd=_get_float(src, "TRADER_MAX_SPEND_PER_CYCLE_USD", 0.0),
        max_spend_per_day_usd=_get_float(src, "TRADER_MAX_SPEND_PER_DAY_USD", 0.0),
        max_open_positions=_get_int(src, "TRADER_MAX_OPEN_POSITIONS", 0),
        max_consecutive_failures=_get_int(src, "TRADER_MAX_CONSECUTIVE_FAILURES", 0),
        offer_bump_usd=_get_float(src, "TRADER_OFFER_BUMP_USD", 0.10),
        offer_bump_age_hours=_get_float(src, "TRADER_OFFER_BUMP_AGE_HOURS", 24.0),
        offer_bump_max=_get_int(src, "TRADER_OFFER_BUMP_MAX", 3),
        min_operate_usd=_get_float(src, "TRADER_MIN_OPERATE_USD", 0.0),
        max_owned_cards=_get_int(src, "TRADER_MAX_OWNED_CARDS", 0),
        unpopular_days=_get_float(src, "TRADER_UNPOPULAR_DAYS", 7.0),
        markdown_delay_days=_get_float(src, "TRADER_MARKDOWN_DELAY_DAYS", 3.0),
        markdown_step_pct=_get_float(src, "TRADER_MARKDOWN_STEP_PCT", 1.0),
        markdown_interval_days=_get_float(src, "TRADER_MARKDOWN_INTERVAL_DAYS", 3.0),
        offer_accept_delay_days=_get_float(src, "TRADER_OFFER_ACCEPT_DELAY_DAYS", 3.0),
        offer_accept_min_market_pct=_get_float(src, "TRADER_OFFER_ACCEPT_MIN_MARKET_PCT", 0.0),
        market_recheck_hours=_get_float(src, "TRADER_MARKET_RECHECK_HOURS", 24.0),
        categories=_get_tuple(src, "TRADER_CATEGORIES", ("",)),
        max_pages=_get_int(src, "TRADER_MAX_PAGES", 10),
        allowed_marketplaces=_get_tuple(src, "TRADER_ALLOWED_MARKETPLACES", ("CC",)),
        loop_interval_sec=_get_float(src, "TRADER_LOOP_INTERVAL_SEC", 300.0),
        # Audit paths: an ABSENT env var falls back to the default location; an
        # explicitly-set EMPTY value disables that record (so it can be turned
        # off deliberately, which _get_str's "empty == default" cannot express).
        ledger_path=os.environ.get(
            "TRADER_LEDGER_PATH", "records/transactions.csv").strip(),
        log_path=os.environ.get("TRADER_LOG_PATH", "logs/bot.log").strip(),
        auto_resume=_get_bool(os.environ, "TRADER_AUTO_RESUME", False),
    )
