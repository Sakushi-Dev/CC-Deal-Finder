"""UI-editable trader settings.

Strategy tuning lives in a local, git-ignored ``trader_settings.json`` (the
primary store for these knobs); a template ships as
``trader_settings.example.json``. The trader UI edits the same file. Values are
layered on top of the environment by
:func:`collectorcrypt.trader.config.load_config`, so this file wins over any
leftover ``.env`` value.

Security / connection variables are deliberately **not** editable here:
the wallet **private key** (``TRADER_WALLET_SECRET``), the live master switch
(``TRADER_LIVE``), the auth provider/credentials, ``TRADER_AUTO_RESUME`` and the
connection settings (``TRADER_RPC_URL``, ``TRADER_WALLET_ADDRESS``) stay in
``.env`` and are read from the environment only, so a real-spending toggle can
never be flipped from the web UI and the connection is visible at a glance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import config as app_config

# Stored next to the project root (cwd when the app runs). Git-ignored.
OVERRIDES_PATH = Path(os.environ.get("TRADER_SETTINGS_PATH", "trader_settings.json"))


# Field specs drive both the UI form and validation. ``type`` is one of
# "number" | "text" | "csv". Numbers carry optional min/max/step hints.
EDITABLE_FIELDS: list[dict[str, Any]] = [
    {"env": "TRADER_RESERVE_USDC", "label": "USDC reserve", "type": "number",
     "min": 0, "step": 1, "group": "Budget",
     "help": "USDC never spent. Available volume = balance - reserve."},
    {"env": "TRADER_GAS_RESERVE_SOL", "label": "SOL gas reserve", "type": "number",
     "min": 0, "step": 0.01, "group": "Budget",
     "help": "SOL kept for transaction fees."},

    {"env": "TRADER_BASE_MAX_CARD_USD", "label": "Base per-card cap (USD)",
     "type": "number", "min": 0, "step": 1, "group": "Sizing",
     "help": "Default max price per card. Keep low for quantity."},
    {"env": "TRADER_MIN_CARD_USD", "label": "Min card value (USD)",
     "type": "number", "min": 0, "step": 1, "group": "Sizing",
     "help": "Only trade cards whose market (insured) value is at least this. "
             "Valuable cards resell better and carry less liquidity risk. "
             "0 = no minimum."},
    {"env": "TRADER_MIN_DISCOUNT_PCT", "label": "Min discount %", "type": "number",
     "min": 0, "max": 100, "step": 1, "group": "Sizing",
     "help": "Only buy when ask is at least this % below insured value."},

    {"env": "TRADER_DIRECT_BUY_PCT", "label": "Direct-buy % of volume",
     "type": "number", "min": 0, "max": 100, "step": 1, "group": "Allocation",
     "help": "Share of volume used for instant purchases. Direct% + Offer% "
             "should not exceed 100 (if it does, both are scaled down)."},
    {"env": "TRADER_OFFER_PCT", "label": "Offer % of volume", "type": "number",
     "min": 0, "max": 100, "step": 1, "group": "Allocation",
     "help": "Share of volume used for standing buy orders (offers). Set Direct% "
             "to 0 to put the whole volume into offers."},
    {"env": "TRADER_OFFER_DISCOUNT_PCT", "label": "Offer discount % below ask",
     "type": "number", "min": 0, "max": 100, "step": 1, "group": "Allocation",
     "help": "How far below the ask price an offer is placed."},
    {"env": "TRADER_OFFER_MAX_PREMIUM_PCT", "label": "Offer max premium % over market",
     "type": "number", "min": 0, "max": 100, "step": 1, "group": "Allocation",
     "help": "Offers ignore the minimum discount, but skip listings priced more "
             "than this far above market value — such sellers expect a profit "
             "and rarely accept a lowball offer (e.g. 10%)."},

    {"env": "TRADER_RESELL_DISCOUNT_PCT", "label": "Resell discount % below market",
     "type": "number", "min": 0, "max": 100, "step": 1, "group": "Resale",
     "help": "Relist bought cards this far below market value. Must be smaller "
             "than the buy discount so every sale is profitable (e.g. buy -30%, "
             "sell -10%)."},

    {"env": "TRADER_ESCALATION_VOLUME_USD", "label": "Escalation volume (USD)",
     "type": "number", "min": 0, "step": 1, "group": "Escalation",
     "help": "When available volume reaches this, raise the per-card cap."},
    {"env": "TRADER_ESCALATION_MAX_CARD_USD", "label": "Escalated per-card cap (USD)",
     "type": "number", "min": 0, "step": 1, "group": "Escalation",
     "help": "Per-card cap while escalation is active."},

    {"env": "TRADER_MAX_SPEND_PER_CYCLE_USD", "label": "Max spend per cycle (USD)",
     "type": "number", "min": 0, "step": 1, "group": "Risk limits",
     "help": "Hard ceiling on USD committed in one cycle. 0 = no limit. "
             "Orders beyond the cap are blocked, not sent (live only)."},
    {"env": "TRADER_MAX_SPEND_PER_DAY_USD", "label": "Max spend per day (USD)",
     "type": "number", "min": 0, "step": 1, "group": "Risk limits",
     "help": "Rolling 24h ceiling on realized USD spend across cycles. "
             "0 = no limit."},
    {"env": "TRADER_MAX_OPEN_POSITIONS", "label": "Max open positions",
     "type": "number", "min": 0, "step": 1, "group": "Risk limits",
     "help": "Cap on concurrent in-flight orders. 0 = no limit."},
    {"env": "TRADER_MAX_CONSECUTIVE_FAILURES", "label": "Kill switch: consecutive failures",
     "type": "number", "min": 0, "step": 1, "group": "Risk limits",
     "help": "Halt all trading after this many real orders fail in a row "
             "(an anomaly signal). 0 = disabled."},

    {"env": "TRADER_OFFER_BUMP_USD", "label": "Offer bump amount (USDC)",
     "type": "number", "min": 0, "step": 0.01, "group": "Offers",
     "help": "How much to raise an aged open offer to re-trigger the owner's "
             "notification (e.g. 0.10 USDC)."},
    {"env": "TRADER_OFFER_BUMP_AGE_HOURS", "label": "Offer age before bump (hours)",
     "type": "number", "min": 0, "step": 1, "group": "Offers",
     "help": "An open offer older than this is bumped once more."},
    {"env": "TRADER_OFFER_BUMP_MAX", "label": "Max offer bumps",
     "type": "number", "min": 0, "step": 1, "group": "Offers",
     "help": "After this many bumps with no reaction, the offer is cancelled "
             "(escrow refunds)."},

    {"env": "TRADER_MIN_OPERATE_USD", "label": "Min operating volume (USDC)",
     "type": "number", "min": 0, "step": 1, "group": "Holdings",
     "help": "Below this available volume the bot stops new buys/offers but "
             "keeps managing inventory it already owns. 0 = disabled."},
    {"env": "TRADER_MAX_OWNED_CARDS", "label": "Max owned cards",
     "type": "number", "min": 0, "step": 1, "group": "Holdings",
     "help": "Cap on actually held (unsold) cards. 0 = disabled."},
    {"env": "TRADER_UNPOPULAR_DAYS", "label": "Days unsold → blacklist",
     "type": "number", "min": 0, "step": 1, "group": "Holdings",
     "help": "A held card unsold this long is flagged unpopular and never "
             "bought/offered again (clearable in the UI)."},
    {"env": "TRADER_MARKDOWN_DELAY_DAYS", "label": "Days unsold → start markdown",
     "type": "number", "min": 0, "step": 1, "group": "Holdings",
     "help": "How long a listed card may sit before the markdown curve starts."},
    {"env": "TRADER_MARKDOWN_STEP_PCT", "label": "Markdown step % (of buy market value)",
     "type": "number", "min": 0, "max": 100, "step": 0.5, "group": "Holdings",
     "help": "Each markdown step lowers the price by this % of the card's "
             "market value at buy. Never goes below cost (0% profit floor)."},
    {"env": "TRADER_MARKDOWN_INTERVAL_DAYS", "label": "Days between markdown steps",
     "type": "number", "min": 0, "step": 1, "group": "Holdings",
     "help": "Spacing between successive markdown steps."},
    {"env": "TRADER_OFFER_ACCEPT_DELAY_DAYS", "label": "Days after floor → accept offers",
     "type": "number", "min": 0, "step": 1, "group": "Holdings",
     "help": "Once at the cost floor for this long, the best incoming offer "
             "may be accepted."},
    {"env": "TRADER_OFFER_ACCEPT_MIN_MARKET_PCT", "label": "Min market % to accept a bid",
     "type": "number", "min": 0, "max": 100, "step": 1, "group": "Holdings",
     "help": "An incoming offer is only accepted if it is at least this % of "
             "market value. 0 = disabled (accept any)."},
    {"env": "TRADER_MARKET_RECHECK_HOURS", "label": "Held-card market re-check (hours)",
     "type": "number", "min": 0, "step": 1, "group": "Holdings",
     "help": "How often a held card's current market value is re-checked. A "
             "rise raises the resale price and restarts the sell cycle."},

    {"env": "TRADER_CATEGORIES", "label": "Categories", "type": "multiselect",
     "options": [c for c in app_config.SCAN_CATEGORIES if c], "group": "Sourcing",
     "help": "Tick the categories to scan. None selected = all categories."},
    {"env": "TRADER_MAX_PAGES", "label": "Max pages per cycle", "type": "number",
     "min": 1, "max": 200, "step": 1, "group": "Sourcing",
     "help": "Marketplace pages scanned each cycle."},
    {"env": "TRADER_ALLOWED_MARKETPLACES", "label": "Allowed marketplaces",
     "type": "csv", "group": "Sourcing",
     "help": "CC only by default. Magic Eden (ME) is ignored."},

    # Persisted by the "Loop every" control on the dashboard, not shown as a
    # separate settings field ("hidden"). Kept editable so it can be saved.
    {"env": "TRADER_LOOP_INTERVAL_SEC", "label": "Loop interval (seconds)",
     "type": "number", "min": 15, "step": 15, "group": "Loop", "hidden": True,
     "help": "Wait between automatic cycles when the loop is running."},
]

_EDITABLE_ENV = {f["env"] for f in EDITABLE_FIELDS}


# --------------------------------------------------------------------------- #
# Strategy presets
# --------------------------------------------------------------------------- #
# Built-in, read-only profiles. Each only sets the *strategy* knobs (allocation,
# discounts, offer psychology, markdown curve); budget, risk caps and connection
# settings are left untouched so a preset never overwrites a user's wallet
# reserves or kill-switch. Applying a preset merges these over the current
# values, so anything not listed here is kept.
BUILTIN_PRESETS: list[dict[str, Any]] = [
    {
        "id": "direct_flip",
        "label": "Direct flip — buy & sell only",
        "description": (
            "100% direct buys, no offers. Only snaps up clear bargains (deep "
            "discount) and relists just under market so you are the cheapest "
            "comparable — buyers anchor on the lowest price, so an undercut "
            "sells fastest. Frequent small markdowns keep capital turning over "
            "instead of locked in unsold cards."
        ),
        "values": {
            "TRADER_DIRECT_BUY_PCT": "100",
            "TRADER_OFFER_PCT": "0",
            "TRADER_MIN_DISCOUNT_PCT": "30",
            "TRADER_RESELL_DISCOUNT_PCT": "8",
            "TRADER_MARKDOWN_DELAY_DAYS": "3",
            "TRADER_MARKDOWN_STEP_PCT": "5",
            "TRADER_MARKDOWN_INTERVAL_DAYS": "2",
            "TRADER_OFFER_ACCEPT_DELAY_DAYS": "7",
            "TRADER_OFFER_ACCEPT_MIN_MARKET_PCT": "90",
        },
    },
    {
        "id": "balanced",
        "label": "Balanced 50 / 50",
        "description": (
            "Half the volume buys instantly, half rests as offers — hedging "
            "speed against price. Offers sit a meaningful but non-insulting "
            "step below ask (a credible anchor the seller can still feel good "
            "accepting) and are gently bumped to re-surface in the seller's "
            "notifications (mere-exposure nudging) before being cancelled."
        ),
        "values": {
            "TRADER_DIRECT_BUY_PCT": "50",
            "TRADER_OFFER_PCT": "50",
            "TRADER_MIN_DISCOUNT_PCT": "25",
            "TRADER_OFFER_DISCOUNT_PCT": "15",
            "TRADER_OFFER_MAX_PREMIUM_PCT": "10",
            "TRADER_RESELL_DISCOUNT_PCT": "10",
            "TRADER_MARKDOWN_DELAY_DAYS": "5",
            "TRADER_MARKDOWN_STEP_PCT": "5",
            "TRADER_MARKDOWN_INTERVAL_DAYS": "3",
            "TRADER_OFFER_BUMP_USD": "0.1",
            "TRADER_OFFER_BUMP_AGE_HOURS": "24",
            "TRADER_OFFER_BUMP_MAX": "3",
            "TRADER_OFFER_ACCEPT_DELAY_DAYS": "5",
            "TRADER_OFFER_ACCEPT_MIN_MARKET_PCT": "85",
        },
    },
    {
        "id": "patient_offers",
        "label": "Patient offers — offers & sell only",
        "description": (
            "100% standing offers, no instant buys. Lowball bids set a wide "
            "anchor far below ask; you only need a fraction to land. Persistent "
            "bumps repeatedly ping the seller (loss-aversion + nagging — a "
            "resting bid they keep seeing is a bird in the hand). Skips sellers "
            "priced well above market, who almost never accept a lowball."
        ),
        "values": {
            "TRADER_DIRECT_BUY_PCT": "0",
            "TRADER_OFFER_PCT": "100",
            "TRADER_OFFER_DISCOUNT_PCT": "25",
            "TRADER_OFFER_MAX_PREMIUM_PCT": "5",
            "TRADER_RESELL_DISCOUNT_PCT": "10",
            "TRADER_OFFER_BUMP_USD": "0.1",
            "TRADER_OFFER_BUMP_AGE_HOURS": "18",
            "TRADER_OFFER_BUMP_MAX": "5",
            "TRADER_MARKDOWN_DELAY_DAYS": "7",
            "TRADER_MARKDOWN_STEP_PCT": "4",
            "TRADER_MARKDOWN_INTERVAL_DAYS": "4",
            "TRADER_OFFER_ACCEPT_DELAY_DAYS": "5",
            "TRADER_OFFER_ACCEPT_MIN_MARKET_PCT": "85",
        },
    },
]

_PRESET_BY_ID = {p["id"]: p for p in BUILTIN_PRESETS}

# User-saved profiles live next to the overrides file. Git-ignored.
PROFILES_PATH = Path(os.environ.get("TRADER_PROFILES_PATH", "trader_profiles.json"))



def load_overrides() -> dict[str, str]:
    """Return the saved overrides (``{}`` if the file is missing/invalid)."""
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Only keep known, editable keys as strings.
    return {k: str(v) for k, v in raw.items()
            if k in _EDITABLE_ENV and v is not None}


def save_overrides(values: dict[str, Any]) -> dict[str, str]:
    """Validate + persist overrides, merged over any existing ones.

    Merging means a caller can save a single key (e.g. the loop interval)
    without wiping settings it did not include.
    """
    clean = _validate(values)
    merged = {**load_overrides(), **clean}
    OVERRIDES_PATH.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return merged


def current_settings() -> list[dict[str, Any]]:
    """Field specs with their effective current values (env + overrides).

    Hidden fields (persisted but driven by a dedicated dashboard control) are
    skipped so they don't appear twice in the settings form.
    """
    overrides = load_overrides()
    out: list[dict[str, Any]] = []
    for field in EDITABLE_FIELDS:
        if field.get("hidden"):
            continue
        env = field["env"]
        value = overrides.get(env, os.environ.get(env, ""))
        out.append({**field, "value": value})
    return out


def _validate(values: dict[str, Any]) -> dict[str, str]:
    spec_by_env = {f["env"]: f for f in EDITABLE_FIELDS}
    clean: dict[str, str] = {}
    for env, raw in values.items():
        spec = spec_by_env.get(env)
        if spec is None:
            continue  # ignore unknown / non-editable keys (e.g. secret)
        text = "" if raw is None else str(raw).strip()
        if spec["type"] == "number" and text != "":
            try:
                num = float(text)
            except ValueError as exc:
                raise ValueError(f"{spec['label']}: not a number") from exc
            if "min" in spec and num < spec["min"]:
                raise ValueError(f"{spec['label']}: must be >= {spec['min']}")
            if "max" in spec and num > spec["max"]:
                raise ValueError(f"{spec['label']}: must be <= {spec['max']}")
            text = str(int(num)) if num.is_integer() else str(num)
        clean[env] = text
    return clean


# --------------------------------------------------------------------------- #
# Profiles (built-in presets + user-saved snapshots)
# --------------------------------------------------------------------------- #
def _effective_values() -> dict[str, str]:
    """Current effective value of every editable field (env + overrides)."""
    overrides = load_overrides()
    out: dict[str, str] = {}
    for field in EDITABLE_FIELDS:
        env = field["env"]
        out[env] = overrides.get(env, os.environ.get(env, ""))
    return out


def load_user_profiles() -> dict[str, dict[str, str]]:
    """Return saved user profiles (``{}`` if the file is missing/invalid)."""
    try:
        raw = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for name, values in raw.items():
        if not isinstance(values, dict):
            continue
        out[str(name)] = {k: str(v) for k, v in values.items()
                          if k in _EDITABLE_ENV and v is not None}
    return out


def _write_user_profiles(profiles: dict[str, dict[str, str]]) -> None:
    PROFILES_PATH.write_text(
        json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def list_profiles() -> dict[str, Any]:
    """Presets + saved profiles for the UI selector."""
    return {
        "presets": [
            {"id": p["id"], "label": p["label"], "description": p["description"]}
            for p in BUILTIN_PRESETS
        ],
        "custom": sorted(load_user_profiles().keys()),
    }


def apply_preset(preset_id: str) -> dict[str, str]:
    """Merge a built-in preset's strategy values over the current overrides."""
    preset = _PRESET_BY_ID.get(preset_id)
    if preset is None:
        raise ValueError(f"unknown preset: {preset_id}")
    return save_overrides(dict(preset["values"]))


def apply_user_profile(name: str) -> dict[str, str]:
    """Apply a saved user profile (its values become the live overrides)."""
    profile = load_user_profiles().get(name)
    if profile is None:
        raise ValueError(f"unknown profile: {name}")
    return save_overrides(dict(profile))


def save_user_profile(name: str) -> list[str]:
    """Snapshot the current effective settings under ``name``.

    Returns the sorted list of profile names after saving.
    """
    clean = (name or "").strip()
    if not clean:
        raise ValueError("profile name is required")
    if len(clean) > 60:
        raise ValueError("profile name is too long (max 60 chars)")
    if clean in _PRESET_BY_ID:
        raise ValueError("that name is reserved for a built-in preset")
    profiles = load_user_profiles()
    profiles[clean] = _effective_values()
    _write_user_profiles(profiles)
    return sorted(profiles.keys())


def delete_user_profile(name: str) -> list[str]:
    """Delete a saved user profile. Returns the remaining names."""
    profiles = load_user_profiles()
    if name in profiles:
        del profiles[name]
        _write_user_profiles(profiles)
    return sorted(profiles.keys())

