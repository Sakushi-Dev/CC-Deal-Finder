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


def test_audit_paths_default_when_absent(clean_env, settings_file):
    cfg = load_config()
    assert cfg.ledger_path == "records/transactions.csv"
    assert cfg.log_path == "logs/bot.log"


def test_audit_paths_empty_env_disables(clean_env, settings_file):
    # An explicitly-empty value disables the record (no default fallback).
    clean_env.setenv("TRADER_LEDGER_PATH", "")
    clean_env.setenv("TRADER_LOG_PATH", "")
    cfg = load_config()
    assert cfg.ledger_path == ""
    assert cfg.log_path == ""


def test_audit_paths_env_override(clean_env, settings_file):
    clean_env.setenv("TRADER_LEDGER_PATH", "/tmp/led.csv")
    clean_env.setenv("TRADER_LOG_PATH", "/tmp/bot.log")
    cfg = load_config()
    assert cfg.ledger_path == "/tmp/led.csv"
    assert cfg.log_path == "/tmp/bot.log"


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
# Holdings lifecycle tunables (Etappe 2) — defaults + JSON overrides
# --------------------------------------------------------------------------- #
def test_holdings_tunable_defaults(clean_env, settings_file):
    cfg = load_config()
    assert cfg.offer_bump_usd == 0.10
    assert cfg.offer_bump_age_hours == 24.0
    assert cfg.offer_bump_max == 3
    assert cfg.min_operate_usd == 0.0
    assert cfg.max_owned_cards == 0
    assert cfg.unpopular_days == 7.0
    assert cfg.markdown_delay_days == 3.0
    assert cfg.markdown_step_pct == 1.0
    assert cfg.markdown_interval_days == 3.0
    assert cfg.offer_accept_delay_days == 3.0
    assert cfg.offer_accept_min_market_pct == 0.0
    assert cfg.market_recheck_hours == 24.0


def test_holdings_tunables_from_json(clean_env, settings_file):
    settings_file.write_text(json.dumps({
        "TRADER_OFFER_BUMP_USD": "0.25",
        "TRADER_OFFER_BUMP_AGE_HOURS": "12",
        "TRADER_OFFER_BUMP_MAX": "5",
        "TRADER_MIN_OPERATE_USD": "50",
        "TRADER_MAX_OWNED_CARDS": "20",
        "TRADER_UNPOPULAR_DAYS": "10",
        "TRADER_MARKDOWN_DELAY_DAYS": "2",
        "TRADER_MARKDOWN_STEP_PCT": "2.5",
        "TRADER_MARKDOWN_INTERVAL_DAYS": "4",
        "TRADER_OFFER_ACCEPT_DELAY_DAYS": "5",
        "TRADER_OFFER_ACCEPT_MIN_MARKET_PCT": "80",
        "TRADER_MARKET_RECHECK_HOURS": "6",
    }))
    cfg = load_config()
    assert cfg.offer_bump_usd == 0.25
    assert cfg.offer_bump_age_hours == 12.0
    assert cfg.offer_bump_max == 5
    assert cfg.min_operate_usd == 50.0
    assert cfg.max_owned_cards == 20
    assert cfg.unpopular_days == 10.0
    assert cfg.markdown_delay_days == 2.0
    assert cfg.markdown_step_pct == 2.5
    assert cfg.markdown_interval_days == 4.0
    assert cfg.offer_accept_delay_days == 5.0
    assert cfg.offer_accept_min_market_pct == 80.0
    assert cfg.market_recheck_hours == 6.0


def test_offer_bump_max_is_int(clean_env, settings_file):
    settings_file.write_text(json.dumps({"TRADER_OFFER_BUMP_MAX": "4"}))
    val = load_config().offer_bump_max
    assert isinstance(val, int)
    assert val == 4


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
    "TRADER_OFFER_BUMP_USD", "TRADER_OFFER_BUMP_AGE_HOURS",
    "TRADER_OFFER_BUMP_MAX", "TRADER_MIN_OPERATE_USD",
    "TRADER_MAX_OWNED_CARDS", "TRADER_UNPOPULAR_DAYS",
    "TRADER_MARKDOWN_DELAY_DAYS", "TRADER_MARKDOWN_STEP_PCT",
    "TRADER_MARKDOWN_INTERVAL_DAYS", "TRADER_OFFER_ACCEPT_DELAY_DAYS",
    "TRADER_OFFER_ACCEPT_MIN_MARKET_PCT", "TRADER_MARKET_RECHECK_HOURS",
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


# --------------------------------------------------------------------------- #
# Strategy profiles (built-in presets + user-saved snapshots)
# --------------------------------------------------------------------------- #
@pytest.fixture
def profiles_file(tmp_path, monkeypatch):
    """Point the user-profiles file at an isolated temp path."""
    path = tmp_path / "trader_profiles.json"
    monkeypatch.setenv("TRADER_PROFILES_PATH", str(path))
    monkeypatch.setattr(settingsmod, "PROFILES_PATH", path)
    return path


def test_builtin_presets_have_expected_ids():
    ids = {p["id"] for p in settingsmod.BUILTIN_PRESETS}
    assert ids == {"direct_flip", "balanced", "patient_offers"}


def test_presets_only_reference_editable_keys():
    for preset in settingsmod.BUILTIN_PRESETS:
        for env in preset["values"]:
            assert env in settingsmod._EDITABLE_ENV


def test_presets_define_the_full_strategy_knob_set():
    # Every preset must set the complete set of strategy knobs so switching
    # profiles is deterministic (no leftover value bleeds through).
    expected = set(settingsmod.PRESET_KEYS)
    assert expected  # guard against an empty derivation
    for preset in settingsmod.BUILTIN_PRESETS:
        assert set(preset["values"]) == expected, preset["id"]


def test_presets_never_touch_budget_risk_sourcing_or_loop():
    # Budget reserves, risk caps (kill switch), sourcing (categories) and the
    # loop interval are the user's domain — a preset must never overwrite them.
    protected = {
        f["env"] for f in settingsmod.EDITABLE_FIELDS
        if f.get("group") in {"Budget", "Risk limits", "Sourcing", "Loop"}
    }
    for preset in settingsmod.BUILTIN_PRESETS:
        assert protected.isdisjoint(preset["values"]), preset["id"]


def test_preset_allocations_are_consistent():
    by_id = {p["id"]: p["values"] for p in settingsmod.BUILTIN_PRESETS}
    assert by_id["direct_flip"]["TRADER_DIRECT_BUY_PCT"] == "100"
    assert by_id["direct_flip"]["TRADER_OFFER_PCT"] == "0"
    assert by_id["balanced"]["TRADER_DIRECT_BUY_PCT"] == "50"
    assert by_id["balanced"]["TRADER_OFFER_PCT"] == "50"
    assert by_id["patient_offers"]["TRADER_DIRECT_BUY_PCT"] == "0"
    assert by_id["patient_offers"]["TRADER_OFFER_PCT"] == "100"


def test_apply_preset_persists_values(clean_env, settings_file, profiles_file):
    settingsmod.apply_preset("patient_offers")
    overrides = settingsmod.load_overrides()
    assert overrides["TRADER_OFFER_PCT"] == "100"
    assert overrides["TRADER_DIRECT_BUY_PCT"] == "0"


def test_apply_preset_merges_over_existing(clean_env, settings_file, profiles_file):
    settingsmod.save_overrides({"TRADER_RESERVE_USDC": "42"})
    settingsmod.apply_preset("balanced")
    overrides = settingsmod.load_overrides()
    assert overrides["TRADER_RESERVE_USDC"] == "42"  # untouched by the preset
    assert overrides["TRADER_DIRECT_BUY_PCT"] == "50"


def test_apply_unknown_preset_raises(clean_env, settings_file, profiles_file):
    with pytest.raises(ValueError):
        settingsmod.apply_preset("does_not_exist")


def test_save_and_apply_user_profile(clean_env, settings_file, profiles_file):
    settingsmod.save_overrides({"TRADER_MIN_DISCOUNT_PCT": "33"})
    settingsmod.save_user_profile("My profile")
    # Change the live setting, then restore via the saved profile.
    settingsmod.save_overrides({"TRADER_MIN_DISCOUNT_PCT": "10"})
    settingsmod.apply_user_profile("My profile")
    assert settingsmod.load_overrides()["TRADER_MIN_DISCOUNT_PCT"] == "33"


def test_save_user_profile_rejects_blank(clean_env, settings_file, profiles_file):
    with pytest.raises(ValueError):
        settingsmod.save_user_profile("   ")


def test_save_user_profile_rejects_preset_name(clean_env, settings_file, profiles_file):
    with pytest.raises(ValueError):
        settingsmod.save_user_profile("balanced")


def test_delete_user_profile(clean_env, settings_file, profiles_file):
    settingsmod.save_user_profile("A")
    settingsmod.save_user_profile("B")
    remaining = settingsmod.delete_user_profile("A")
    assert remaining == ["B"]


def test_list_profiles_shape(clean_env, settings_file, profiles_file):
    settingsmod.save_user_profile("Custom one")
    listing = settingsmod.list_profiles()
    assert [p["id"] for p in listing["presets"]] == \
        ["direct_flip", "balanced", "patient_offers"]
    assert listing["custom"] == ["Custom one"]


def test_load_user_profiles_ignores_invalid(profiles_file):
    profiles_file.write_text("{ not valid json")
    assert settingsmod.load_user_profiles() == {}


# --------------------------------------------------------------------------- #
# trader_settings.example.json must stay in sync with EDITABLE_FIELDS
# --------------------------------------------------------------------------- #
def test_example_settings_file_lists_every_editable_field():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    example = json.loads(
        (root / "trader_settings.example.json").read_text(encoding="utf-8")
    )
    keys = {k for k in example if k != "_comment"}
    editable = {f["env"] for f in settingsmod.EDITABLE_FIELDS}
    assert keys == editable, (
        f"missing: {sorted(editable - keys)} extra: {sorted(keys - editable)}"
    )


