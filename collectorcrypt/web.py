"""Flask app factory + routes.

Two blueprints:
    views   – HTML pages (`/`, `/deals`).
    api     – JSON endpoints (`/api/card/<nft>`, `/deals/*`).
"""
from __future__ import annotations

import math
import re
from typing import Any

import requests
from flask import Blueprint, Flask, current_app, jsonify, render_template, request

from . import config
from .api import CCClient
from .normalize import normalize_card
from .scanner import ScanManager
from .trader import TraderManager, Wallet, WalletError, load_config
from .trader import settings as trader_settings


_NFT_RE = re.compile(r"[A-Za-z0-9_-]{20,80}")
_WALLET_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")  # Base58 (Solana)


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    client = CCClient()
    scanner = ScanManager(client)
    app.extensions["cc_client"] = client
    app.extensions["cc_scanner"] = scanner
    app.extensions["cc_trader"] = TraderManager()

    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)
    return app


def _client() -> CCClient:
    return current_app.extensions["cc_client"]


def _scanner() -> ScanManager:
    return current_app.extensions["cc_scanner"]


def _trader() -> TraderManager:
    return current_app.extensions["cc_trader"]


# --------------------------------------------------------------------------- #
# HTML pages
# --------------------------------------------------------------------------- #
views_bp = Blueprint("views", __name__)


@views_bp.route("/")
def index():
    page = max(1, request.args.get("page", default=1, type=int))
    step = min(
        config.MAX_STEP,
        max(config.MIN_STEP, request.args.get("step", default=config.DEFAULT_STEP, type=int)),
    )
    search = (request.args.get("q") or "").strip()

    error: str | None = None
    cards: list[dict[str, Any]] = []
    total = total_pages = found = 0
    try:
        data = _client().fetch_marketplace_page(page, step, search)
        cards = [normalize_card(c) for c in (data.get("filterNFtCard") or [])]
        found = int(data.get("findTotal") or 0)
        total = int(data.get("total") or 0)
        total_pages = int(data.get("totalPages") or math.ceil(found / step) or 1)
    except requests.RequestException as exc:
        error = f"API error: {exc}"

    return render_template(
        "index.html",
        cards=cards, page=page, step=step, search=search,
        found=found, total=total, total_pages=total_pages, error=error,
    )


@views_bp.route("/deals")
def deals():
    return render_template("deals.html", state=_scanner().snapshot(),
                           categories=config.SCAN_CATEGORIES)


@views_bp.route("/trader")
def trader():
    cfg = load_config()
    return render_template(
        "trader.html",
        state=_trader().snapshot(),
        settings=trader_settings.current_settings(),
        profiles=trader_settings.list_profiles(),
        loop_interval=int(cfg.loop_interval_sec),
    )


@views_bp.route("/profile")
def profile():
    wallet = (request.args.get("wallet") or "").strip()
    page = max(1, request.args.get("page", default=1, type=int))
    step = min(
        config.MAX_STEP,
        max(config.MIN_STEP, request.args.get("step", default=config.DEFAULT_STEP, type=int)),
    )

    error: str | None = None
    cards: list[dict[str, Any]] = []
    total_cards = 0
    total_pages = 0
    insured_sum = 0.0
    qty_by_category: dict[str, int] = {}
    not_found = False

    if wallet:
        if not _WALLET_RE.fullmatch(wallet):
            error = "Invalid Solana wallet address."
        else:
            try:
                data = _client().fetch_wallet_cards(wallet, page, step)
                not_found = bool(data.get("_notFound"))
                cards = [normalize_card(c) for c in (data.get("filterNFtCard") or [])]
                total_cards = int(data.get("totalCards") or data.get("total") or 0)
                total_pages = int(data.get("totalPages") or 0)
                try:
                    insured_sum = float(data.get("insuredValueSum") or 0)
                except (TypeError, ValueError):
                    insured_sum = 0.0
                qty_by_category = dict(data.get("cardsQtyByCategory") or {})
            except requests.RequestException as exc:
                error = f"API error: {exc}"

    return render_template(
        "profile.html",
        wallet=wallet, page=page, step=step,
        cards=cards, total_cards=total_cards, total_pages=total_pages,
        insured_sum=insured_sum, qty_by_category=qty_by_category,
        not_found=not_found, error=error,
    )


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
api_bp = Blueprint("api", __name__)


@api_bp.route("/api/card/<nft>")
def api_card(nft: str):
    nft = (nft or "").strip()
    if not nft or not _NFT_RE.fullmatch(nft):
        return jsonify({"error": "invalid nftAddress"}), 400
    try:
        raw = _client().fetch_card(nft)
        if raw is None:
            return jsonify({"found": False}), 200
        return jsonify({"found": True, "card": normalize_card(raw)})
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@api_bp.route("/api/sol-rate")
def api_sol_rate():
    """Current SOL→USD spot price. Used by the marketplace and profile
    pages to convert SOL-denominated listings before comparing them to
    the (USD-denominated) insured value.
    """
    try:
        rate = _client().fetch_sol_usd()
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "rate": rate})


@api_bp.route("/deals/start", methods=["POST"])
def deals_start():
    try:
        min_usd = float(request.form.get("min", ""))
        max_usd = float(request.form.get("max", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid number."}), 400
    if min_usd > max_usd or min_usd < 0:
        return jsonify({"ok": False, "error": "Invalid price range."}), 400
    order = (request.form.get("order") or "newest").strip().lower()
    if order not in ("shuffle", "newest"):
        order = "newest"
    category = (request.form.get("category") or "").strip()
    if category not in config.SCAN_CATEGORIES:
        category = ""
    sc = _scanner()
    started = sc.start(min_usd, max_usd, order, category)
    return jsonify({"ok": True, "started": started, "state": sc.snapshot()})


@api_bp.route("/deals/status")
def deals_status():
    return jsonify(_scanner().snapshot())


@api_bp.route("/deals/pause", methods=["POST"])
def deals_pause():
    sc = _scanner()
    sc.pause()
    return jsonify({"ok": True, "state": sc.snapshot()})


@api_bp.route("/deals/resume", methods=["POST"])
def deals_resume():
    sc = _scanner()
    sc.resume()
    return jsonify({"ok": True, "state": sc.snapshot()})


@api_bp.route("/deals/stop", methods=["POST"])
def deals_stop():
    sc = _scanner()
    sc.stop()
    return jsonify({"ok": True, "state": sc.snapshot()})


# --------------------------------------------------------------------------- #
# Trader
# --------------------------------------------------------------------------- #
@api_bp.route("/trader/status")
def trader_status():
    return jsonify(_trader().snapshot())


@api_bp.route("/trader/wallet")
def trader_wallet():
    """Live SOL/USDC balances for the configured wallet (read-only).

    Lets the UI show what is available on the saved address immediately,
    without having to run a full trade cycle first.
    """
    cfg = load_config()
    if not cfg.wallet_address and not cfg.wallet_secret:
        return jsonify({"ok": False, "error": "No wallet configured."})
    try:
        w = Wallet(cfg.rpc_url, address=cfg.wallet_address, secret=cfg.wallet_secret)
        sol = w.sol_balance()
        usdc = w.usdc_balance()
    except WalletError as exc:
        return jsonify({"ok": False, "error": str(exc)})
    available = max(0.0, usdc - max(0.0, cfg.reserve_usdc))
    return jsonify({
        "ok": True, "wallet": w.address,
        "sol_balance": sol, "usdc_balance": usdc,
        "available_volume": available, "reserve_usdc": cfg.reserve_usdc,
    })


@api_bp.route("/trader/run", methods=["POST"])
def trader_run():
    started = _trader().run_once()
    return jsonify({"ok": True, "started": started, "state": _trader().snapshot()})


@api_bp.route("/trader/demo", methods=["POST"])
def trader_demo():
    """Run a single hypothetical cycle against a user-supplied USDC volume."""
    try:
        volume = float(request.form.get("volume", "0"))
    except ValueError:
        volume = 0.0
    if volume <= 0:
        return jsonify({"ok": False, "error": "Enter a volume greater than 0."})
    started = _trader().run_demo(volume)
    return jsonify({"ok": True, "started": started, "state": _trader().snapshot()})


@api_bp.route("/trader/loop/start", methods=["POST"])
def trader_loop_start():
    try:
        interval = float(request.form.get("interval", "300"))
    except ValueError:
        interval = 300.0
    # Remember the chosen spacing so the dashboard shows it again next time.
    try:
        trader_settings.save_overrides({"TRADER_LOOP_INTERVAL_SEC": str(interval)})
    except ValueError:
        pass
    _trader().start_loop(interval)
    return jsonify({"ok": True, "state": _trader().snapshot()})


@api_bp.route("/trader/loop/pause", methods=["POST"])
def trader_loop_pause():
    _trader().pause()
    return jsonify({"ok": True, "state": _trader().snapshot()})


@api_bp.route("/trader/loop/resume", methods=["POST"])
def trader_loop_resume():
    _trader().resume()
    return jsonify({"ok": True, "state": _trader().snapshot()})


@api_bp.route("/trader/loop/stop", methods=["POST"])
def trader_loop_stop():
    _trader().stop()
    return jsonify({"ok": True, "state": _trader().snapshot()})


@api_bp.route("/trader/blacklist/clear", methods=["POST"])
def trader_blacklist_clear():
    nft = (request.form.get("nft") or "").strip()
    if not nft or not _NFT_RE.fullmatch(nft):
        return jsonify({"ok": False, "error": "invalid nftAddress"}), 400
    _trader().clear_blacklist_entry(nft)
    return jsonify({"ok": True, "state": _trader().snapshot()})


@api_bp.route("/trader/settings", methods=["GET"])
def trader_settings_get():
    return jsonify({"ok": True, "settings": trader_settings.current_settings()})


@api_bp.route("/trader/settings", methods=["POST"])
def trader_settings_post():
    payload = request.get_json(silent=True) or request.form.to_dict()
    try:
        saved = trader_settings.save_overrides(dict(payload))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "saved": saved,
                    "settings": trader_settings.current_settings()})


@api_bp.route("/trader/profiles", methods=["GET"])
def trader_profiles_get():
    return jsonify({"ok": True, "profiles": trader_settings.list_profiles()})


@api_bp.route("/trader/profiles/apply", methods=["POST"])
def trader_profiles_apply():
    payload = request.get_json(silent=True) or request.form.to_dict()
    kind = (payload.get("type") or "").strip()
    name = (payload.get("name") or "").strip()
    try:
        if kind == "preset":
            trader_settings.apply_preset(name)
        elif kind == "custom":
            trader_settings.apply_user_profile(name)
        else:
            return jsonify({"ok": False, "error": "unknown profile type"}), 400
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "settings": trader_settings.current_settings()})


@api_bp.route("/trader/profiles/save", methods=["POST"])
def trader_profiles_save():
    payload = request.get_json(silent=True) or request.form.to_dict()
    name = (payload.get("name") or "").strip()
    try:
        names = trader_settings.save_user_profile(name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "profiles": trader_settings.list_profiles(),
                    "saved": name, "custom": names})


@api_bp.route("/trader/profiles/delete", methods=["POST"])
def trader_profiles_delete():
    payload = request.get_json(silent=True) or request.form.to_dict()
    name = (payload.get("name") or "").strip()
    names = trader_settings.delete_user_profile(name)
    return jsonify({"ok": True, "profiles": trader_settings.list_profiles(),
                    "custom": names})
