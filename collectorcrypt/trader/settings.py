"""UI-editable trader settings.

The trader UI lets the user change the same knobs that normally live in
``.env``, without touching the file. Overrides are stored in a local,
git-ignored ``trader_settings.json`` and layered on top of the environment by
:func:`collectorcrypt.trader.config.load_config`.

Security: the wallet **private key** (``TRADER_WALLET_SECRET``) and the live
master switch (``TRADER_LIVE``) are deliberately **not** editable here. They
stay in ``.env`` so a real-spending toggle can never be flipped from the web UI.
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
    {"env": "TRADER_RPC_URL", "label": "Solana RPC URL", "type": "text",
     "group": "Connectivity",
     "help": "Mainnet RPC endpoint used to read balances."},
    {"env": "TRADER_WALLET_ADDRESS", "label": "Wallet address", "type": "text",
     "group": "Connectivity",
     "help": "Public address (read-only). Required for dry-run."},

    {"env": "TRADER_RESERVE_USDC", "label": "USDC reserve", "type": "number",
     "min": 0, "step": 1, "group": "Budget",
     "help": "USDC never spent. Available volume = balance - reserve."},
    {"env": "TRADER_GAS_RESERVE_SOL", "label": "SOL gas reserve", "type": "number",
     "min": 0, "step": 0.01, "group": "Budget",
     "help": "SOL kept for transaction fees."},

    {"env": "TRADER_BASE_MAX_CARD_USD", "label": "Base per-card cap (USD)",
     "type": "number", "min": 0, "step": 1, "group": "Sizing",
     "help": "Default max price per card. Keep low for quantity."},
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
