"""Config + settings-split tests.

The split between security/connection (env-only) and tunables (UI-editable JSON)
is a safety boundary: the live master switch and the wallet secret must never be
flippable from the web UI, while strategy knobs in the JSON win over leftover
env values. These tests pin both halves.
"""
from __future__ import annotations

import json

import pytest

from collectorcrypt.trader import config as cfgmod
from collectorcrypt.trader import settings as settingsmod
from collectorcrypt.trader.config import (DEFAULT_RPC_URL, TraderConfig,
                                          _get_bool, _get_float, _get_int,
                                          _get_tuple, load_config)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all TRADER_* env vars so each test starts from a known baseline."""
    import os

    for key in list(os.environ):
        if key.startswith("TRADER_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point the overrides file at an isolated temp path."""
    path = tmp_path / "trader_settings.json"
    monkeypatch.setenv("TRADER_SETTINGS_PATH", str(path))
    monkeypatch.setattr(settingsmod, "OVERRIDES_PATH", path)
    return path


# --------------------------------------------------------------------------- #
# _get_bool — literal "true" only (matches the live switch semantics)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("TRUE", True), ("  true  ", True),
    ("false", False), ("False", False), ("1", False), ("yes", False),
    ("on", False), ("", False), ("t", False),
])
def test_get_bool_literal_true(value, expected):
    assert _get_bool({"K": value}, "K") is expected


def test_get_bool_default_when_missing():
    assert _get_bool({}, "K", default=True) is True
    assert _get_bool({}, "K", default=False) is False


# --------------------------------------------------------------------------- #
# numeric/tuple coercion helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    ("10", 10.0), ("3.5", 3.5), ("", 1.0), ("notnum", 1.0), (None, 1.0),
])
def test_get_float(value, expected):
    src = {} if value is None else {"K": value}
    assert _get_float(src, "K", 1.0) == expected


@pytest.mark.parametrize("value,expected", [
    ("10", 10), ("", 5), ("notnum", 5), ("3.9", 5),
])
def test_get_int(value, expected):
    assert _get_int({"K": value}, "K", 5) == expected


def test_get_tuple_splits_csv():
    assert _get_tuple({"K": "a, b ,c"}, "K", ()) == ("a", "b", "c")


def test_get_tuple_default_when_empty():
    assert _get_tuple({"K": ""}, "K", ("x",)) == ("x",)


# --------------------------------------------------------------------------- #
# Security/connection: env-only
# --------------------------------------------------------------------------- #
def test_live_reads_env_true(clean_env, settings_file):
    clean_env.setenv("TRADER_LIVE", "true")
    assert load_config().live is True


def test_live_defaults_false(clean_env, settings_file):
    assert load_config().live is False


def test_live_ignores_overrides_file(clean_env, settings_file):
    # Even if someone writes TRADER_LIVE into the JSON, it must be ignored.
    settings_file.write_text(json.dumps({"TRADER_LIVE": "true"}))
    assert load_config().live is False


def test_secret_never_from_overrides(clean_env, settings_file):
    settings_file.write_text(json.dumps({"TRADER_WALLET_SECRET": "leaked"}))
    assert load_config().wallet_secret == ""


def test_rpc_url_env_only(clean_env, settings_file):
    clean_env.setenv("TRADER_RPC_URL", "https://custom.rpc")
    settings_file.write_text(json.dumps({"TRADER_RPC_URL": "https://evil.rpc"}))
    cfg = load_config()
    assert cfg.rpc_url == "https://custom.rpc"


def test_rpc_url_default(clean_env, settings_file):
    assert load_config().rpc_url == DEFAULT_RPC_URL


def test_wallet_address_env_only(clean_env, settings_file):
    clean_env.setenv("TRADER_WALLET_ADDRESS", "ADDR_FROM_ENV")
    settings_file.write_text(json.dumps({"TRADER_WALLET_ADDRESS": "ADDR_FROM_JSON"}))
    assert load_config().wallet_address == "ADDR_FROM_ENV"


def test_auth_provider_lowercased(clean_env, settings_file):
    clean_env.setenv("TRADER_AUTH_PROVIDER", "PRIVY")
    assert load_config().auth_provider == "privy"


def test_auto_resume_env_only(clean_env, settings_file):
    clean_env.setenv("TRADER_AUTO_RESUME", "true")
    settings_file.write_text(json.dumps({"TRADER_AUTO_RESUME": "false"}))
    assert load_config().auto_resume is True


# --------------------------------------------------------------------------- #
# Tunables: JSON overrides win over env
# --------------------------------------------------------------------------- #
def test_tunable_json_wins_over_env(clean_env, settings_file):
    clean_env.setenv("TRADER_MIN_DISCOUNT_PCT", "10")
    settings_file.write_text(json.dumps({"TRADER_MIN_DISCOUNT_PCT": "28"}))
    assert load_config().min_discount_pct == 28.0


def test_tunable_falls_back_to_env(clean_env, settings_file):
    clean_env.setenv("TRADER_BASE_MAX_CARD_USD", "40")
    assert load_config().base_max_card_usd == 40.0


def test_tunable_default_when_unset(clean_env, settings_file):
    assert load_config().min_card_usd == 0.0


def test_min_card_usd_from_json(clean_env, settings_file):
    settings_file.write_text(json.dumps({"TRADER_MIN_CARD_USD": "15"}))
    assert load_config().min_card_usd == 15.0


def test_categories_from_json(clean_env, settings_file):
    settings_file.write_text(json.dumps({"TRADER_CATEGORIES": "Pokemon,One Piece"}))
    assert load_config().categories == ("Pokemon", "One Piece")


# --------------------------------------------------------------------------- #
# EDITABLE_FIELDS must NOT expose connection/security keys
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("forbidden", [
    "TRADER_RPC_URL", "TRADER_WALLET_ADDRESS", "TRADER_WALLET_SECRET",
    "TRADER_LIVE", "TRADER_AUTH_PROVIDER", "TRADER_AUTO_RESUME",
    "TRADER_PRIVY_APP_ID", "TRADER_CC_TOKEN",
])
def test_security_keys_not_editable(forbidden):
    assert forbidden not in settingsmod._EDITABLE_ENV


@pytest.mark.parametrize("expected", [
    "TRADER_RESERVE_USDC", "TRADER_BASE_MAX_CARD_USD", "TRADER_MIN_CARD_USD",
    "TRADER_MIN_DISCOUNT_PCT", "TRADER_MAX_SPEND_PER_CYCLE_USD",
    "TRADER_MAX_CONSECUTIVE_FAILURES", "TRADER_CATEGORIES",
])
def test_tunables_are_editable(expected):
    assert expected in settingsmod._EDITABLE_ENV


# --------------------------------------------------------------------------- #
# load_overrides filtering
# --------------------------------------------------------------------------- #
def test_load_overrides_filters_unknown_keys(settings_file):
    settings_file.write_text(json.dumps({
        "TRADER_MIN_DISCOUNT_PCT": "28",
        "_comment": "this is a note",
        "TRADER_WALLET_SECRET": "leaked",
    }))
    overrides = settingsmod.load_overrides()
    assert overrides == {"TRADER_MIN_DISCOUNT_PCT": "28"}


def test_load_overrides_missing_file(settings_file):
    # File does not exist yet.
    assert settingsmod.load_overrides() == {}


def test_load_overrides_invalid_json(settings_file):
    settings_file.write_text("{ not valid json")
    assert settingsmod.load_overrides() == {}


def test_load_overrides_non_dict(settings_file):
    settings_file.write_text(json.dumps([1, 2, 3]))
    assert settingsmod.load_overrides() == {}


def test_load_overrides_coerces_to_string(settings_file):
    settings_file.write_text(json.dumps({"TRADER_MAX_PAGES": 30}))
    overrides = settingsmod.load_overrides()
    assert overrides["TRADER_MAX_PAGES"] == "30"


# --------------------------------------------------------------------------- #
# save_overrides + validation
# --------------------------------------------------------------------------- #
def test_save_overrides_persists(settings_file):
    settingsmod.save_overrides({"TRADER_MIN_DISCOUNT_PCT": "25"})
    assert settingsmod.load_overrides()["TRADER_MIN_DISCOUNT_PCT"] == "25"


def test_save_overrides_merges(settings_file):
    settingsmod.save_overrides({"TRADER_MIN_DISCOUNT_PCT": "25"})
    settingsmod.save_overrides({"TRADER_MAX_PAGES": "30"})
    overrides = settingsmod.load_overrides()
    assert overrides["TRADER_MIN_DISCOUNT_PCT"] == "25"
    assert overrides["TRADER_MAX_PAGES"] == "30"


def test_save_overrides_ignores_secret(settings_file):
    settingsmod.save_overrides({"TRADER_WALLET_SECRET": "leaked",
                               "TRADER_MIN_DISCOUNT_PCT": "25"})
    assert "TRADER_WALLET_SECRET" not in settingsmod.load_overrides()


def test_save_overrides_rejects_non_number(settings_file):
    with pytest.raises(ValueError):
        settingsmod.save_overrides({"TRADER_MIN_DISCOUNT_PCT": "abc"})


def test_save_overrides_enforces_min(settings_file):
    with pytest.raises(ValueError):
        settingsmod.save_overrides({"TRADER_MIN_DISCOUNT_PCT": "-5"})


def test_save_overrides_enforces_max(settings_file):
    with pytest.raises(ValueError):
        settingsmod.save_overrides({"TRADER_MIN_DISCOUNT_PCT": "150"})


def test_save_overrides_normalizes_integer(settings_file):
    merged = settingsmod.save_overrides({"TRADER_MAX_PAGES": "30.0"})
    assert merged["TRADER_MAX_PAGES"] == "30"


# --------------------------------------------------------------------------- #
# Config properties
# --------------------------------------------------------------------------- #
def test_has_secret_property():
    from .conftest import make_config

    assert make_config(wallet_secret="x").has_secret is True
    assert make_config(wallet_secret="").has_secret is False


def test_requires_auth_equals_live():
    from .conftest import make_config

    assert make_config(live=True).requires_auth is True
    assert make_config(live=False).requires_auth is False


def test_config_is_frozen():
    from .conftest import make_config

    cfg = make_config()
    with pytest.raises(Exception):
        cfg.live = True  # frozen dataclass
