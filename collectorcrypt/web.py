"""Flask-App-Factory + Routen.

Zwei Blueprints:
    views   – HTML-Seiten (`/`, `/deals`).
    api     – JSON-Endpoints (`/api/card/<nft>`, `/deals/*`).
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


_NFT_RE = re.compile(r"[A-Za-z0-9_-]{20,80}")


# --------------------------------------------------------------------------- #
# App-Factory
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

    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)
    return app


def _client() -> CCClient:
    return current_app.extensions["cc_client"]


def _scanner() -> ScanManager:
    return current_app.extensions["cc_scanner"]


# --------------------------------------------------------------------------- #
# HTML-Seiten
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
        error = f"API-Fehler: {exc}"

    return render_template(
        "index.html",
        cards=cards, page=page, step=step, search=search,
        found=found, total=total, total_pages=total_pages, error=error,
    )


@views_bp.route("/deals")
def deals():
    return render_template("deals.html", state=_scanner().snapshot())


# --------------------------------------------------------------------------- #
# JSON-API
# --------------------------------------------------------------------------- #
api_bp = Blueprint("api", __name__)


@api_bp.route("/api/card/<nft>")
def api_card(nft: str):
    nft = (nft or "").strip()
    if not nft or not _NFT_RE.fullmatch(nft):
        return jsonify({"error": "ungültige nftAddress"}), 400
    try:
        raw = _client().fetch_card(nft)
        if raw is None:
            return jsonify({"found": False}), 200
        return jsonify({"found": True, "card": normalize_card(raw)})
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@api_bp.route("/deals/start", methods=["POST"])
def deals_start():
    try:
        min_usd = float(request.form.get("min", ""))
        max_usd = float(request.form.get("max", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Ungültige Zahl."}), 400
    if min_usd > max_usd or min_usd < 0:
        return jsonify({"ok": False, "error": "Ungültige Preisspanne."}), 400
    order = (request.form.get("order") or "shuffle").strip().lower()
    if order not in ("shuffle", "newest"):
        order = "shuffle"
    sc = _scanner()
    started = sc.start(min_usd, max_usd, order)
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
