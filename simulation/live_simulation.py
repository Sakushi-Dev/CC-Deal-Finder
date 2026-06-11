"""Standalone 7-day **live** market simulation (compressed, not real time).

This is **not** a pytest test — run it directly:

    .venv\\Scripts\\python.exe simulation\\live_simulation.py

It drives the autonomous trader through :meth:`TradeEngine.run_cycle` in
fully-armed **LIVE** mode for ``168`` cycles — one per simulated hour across
seven days. Time is *compressed*: a ``time.time`` patch makes the bot believe an
hour passes between cycles, so the whole week resolves in a fraction of a second.

At the end it prints **and writes a full log** of everything that happened over
the simulated week (every buy, resting offer, bump, cancel, relist, markdown,
sale, accepted offer, market re-check, risk block and handled anomaly), a
per-day rollup, the final wallet/exchange state and a set of sanity checks.

Outputs
-------
Every invocation creates a self-contained run folder
``simulation/runs/<stamp>/`` holding:

* the human-readable run log -> ``logs/sim_<stamp>.log``;
* the same data as CSV -> ``csv/{cycles,events,daily,summary,checks}_<stamp>.csv``;
* a snapshot of the settings the run used -> ``trade_setting.json``.

``datavisualizing/visualize.py`` discovers these run folders to render a web
dashboard.

The tunable trader settings come from ``simulation/trade_setting.json`` (layered
over the built-in defaults); edit that file to change the values the test runs
with.

Data-contract discipline
-------------------------
Every byte the bot reads is produced **only** in the response shapes verified
from the DevTools captures in ``tools/captures/`` — nothing is invented:

* marketplace / owned-card pages -> the ``dataDesc`` card object;
* card-activity / offer feeds -> a bare JSON array of offer events wrapped by
  the transport as ``{"data": [...]}``;
* every write endpoint -> a bare base64 transaction wrapped as
  ``{"data": "<base64>"}``;
* ``broadcast`` -> ``{"success": true, "signature": "<sig>", "message": ...}``;
* ``check_listing_status`` -> ``{"exists": bool, "marketplace": "CC", ...}``.

The fake exchange is the single authoritative on-chain state. It commits a write
only when its transaction is broadcast (mirroring prepare -> sign -> broadcast),
evolves the market each hour, settles offers/sales and injects realistic
anomalies (an empty page, a rate-limit during sourcing, transient broadcast
500s, a low-balance window).

Exit code is ``0`` when every sanity check passes, ``1`` otherwise.
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from random import Random
from typing import Any

# Make the repository root importable when launched as
# ``python simulation/live_simulation.py`` (the script's own folder, not the
# repo root, is what Python puts on sys.path[0] by default).
_SIM_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SIM_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Output locations (created on demand). Every run gets its own folder under
# ``simulation/runs/<stamp>/`` holding a ``logs/`` and a ``csv/`` sub-directory.
RUNS_DIR = os.path.join(_SIM_DIR, "runs")
SETTINGS_PATH = os.path.join(_SIM_DIR, "trade_setting.json")

from collectorcrypt.trader.auth import AuthSession
from collectorcrypt.trader.ccapi import CCRateLimitError, CCServerError
from collectorcrypt.trader.config import TraderConfig
from collectorcrypt.trader.engine import TradeEngine
from collectorcrypt.trader.store import OrderStore
from collectorcrypt.trader import orders as orders_mod

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_EPOCH = 1_780_000_000  # a fixed 2026 wall-clock anchor for the run
HOURS = 168                 # 7 days x 24h, one cycle per hour
SEED = 20260610

OUR_WALLET = "S1MourWa11etXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
SELLER_WALLET = "S1Mse11erXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
COMPETITOR_WALLET = "S1McompetitorXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
BUYER_WALLET = "S1MincomingbuyerXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

# Scheduled anomalies (hour-of-run -> behaviour).
EMPTY_MARKET_HOUR = 36          # sourcing returns an empty page
RATE_LIMIT_HOUR = 60            # sourcing raises CCRateLimitError (aborts cycle)
BROADCAST_FAIL_HOURS = {3, 4}   # buy/offer broadcasts return a transient 500
LOW_BALANCE_HOURS = {72, 73, 74}  # wallet dips below min-operate -> acquisition paused

# Disposition rotation for confirmed buys (drives the post-buy lifecycle).
_DISPOSITIONS = ("sell_external", "accept_offer", "pump", "hold")


def _iso(epoch: float) -> str:
    """Format an epoch as the ISO ``...Z`` string CC uses in its payloads."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _short(wallet: str) -> str:
    """Abbreviate a wallet for readable log lines."""
    return f"{wallet[:6]}..{wallet[-4:]}" if len(wallet) > 12 else wallet


# --------------------------------------------------------------------------- #
# Verified-shape payload builders
# --------------------------------------------------------------------------- #
def _card_payload(*, nft: str, card_id: str, name: str, insured: float,
                  oracle: float, list_price: float | None, owner_wallet: str,
                  listed_epoch: float) -> dict[str, Any]:
    """Build one ``dataDesc`` card object (marketplace or owned-card shape).

    ``list_price is None`` -> the card is held/unlisted (``listing`` is null);
    otherwise it carries the verified ``listing``/``listings`` objects. Only the
    keys the bot actually reads are populated with simulated scalars; the rest
    mirror the captured shape so the payload is structurally faithful.
    """
    iso = _iso(listed_epoch)
    listing_obj = None
    if list_price is not None:
        listing_obj = {
            "id": f"{card_id}-lst",
            "cardId": card_id,
            "createdAt": iso,
            "currency": "USDC",
            "price": str(round(float(list_price), 2)),
            "receiptId": f"v2_{card_id}",
            "marketplace": "CC",
            "sellerId": owner_wallet,
            "updatedAt": iso,
        }
    return {
        "authenticated": True,
        "blockchain": "Solana",
        "category": "Pokemon",
        "createdAt": iso,
        "lastTransferredAt": iso,
        "listedAt": iso if listing_obj else None,
        "grade": "MINT 9",
        "gradeNum": 9,
        "gradingCompany": "PSA",
        "gradingID": f"grad-{card_id}",
        "id": card_id,
        "insuredValue": str(round(float(insured), 2)),
        "itemName": name,
        "nftAddress": nft,
        "nftStatus": "Valid",
        "ownerId": f"owner-{card_id}",
        "status": "Transferred",
        "oraclePrice": str(round(float(oracle), 2)),
        "type": "Card",
        "updatedAt": iso,
        "year": 2023,
        "set": "Pokemon Svi EN-Scarlet & Violet",
        "images": {
            "front": f"https://img.example.invalid/{card_id}/front",
            "frontS": f"https://img.example.invalid/{card_id}/frontS",
            "back": f"https://img.example.invalid/{card_id}/back",
            "cardId": card_id,
        },
        "listings": [listing_obj] if listing_obj else [],
        "listing": listing_obj,
        "inSwap": False,
        "owner": {
            "id": f"owner-{card_id}",
            "name": "Seller" if owner_wallet != OUR_WALLET else "Us",
            "wallet": owner_wallet,
        },
    }


def _offer_event(*, nft: str, card_id: str, wallet: str, amount: float,
                 epoch: float, action: str = "Offer Made") -> dict[str, Any]:
    """Build one verified card-activity offer event (bare-array element)."""
    raw_amount = str(int(round(float(amount) * 1_000_000)))
    return {
        "action": action,
        "amount": float(amount),
        "priceInfo": {
            "splPrice": {
                "rawAmount": raw_amount,
                "address": USDC_MINT,
                "decimals": 6,
                "symbol": "USDC",
            }
        },
        "card": None,
        "cardId": card_id,
        "collection": None,
        "createdAt": _iso(epoch),
        "from": {"id": f"id-{wallet[:6]}", "name": None, "wallet": wallet},
        "id": f"evt-{nft}-{int(epoch)}-{int(amount)}",
        "ownerId": "",
        "to": {"id": None, "name": None, "wallet": None},
        "nftAddress": nft,
        "transactionUrl": f"https://solscan.io/tx/sim-{nft}-{int(epoch)}",
        "instructionName": "make_offer",
        "source": "COLLECTOR_CRYPT",
    }


# --------------------------------------------------------------------------- #
# Coverage tracker — every major lifecycle branch must fire at least once
# --------------------------------------------------------------------------- #
@dataclass
class Coverage:
    buys_confirmed: int = 0
    offers_opened: int = 0
    offers_bumped: int = 0
    offers_cancelled: int = 0
    relisted: int = 0
    markdowns: int = 0
    sold_detected: int = 0
    offers_accepted: int = 0
    offer_fill_confirmed: int = 0
    risk_blocked: int = 0
    dynamic_reprice: int = 0
    reprice_skipped: int = 0
    market_recheck_raised: int = 0
    escalation: int = 0
    acquisition_paused: int = 0
    cycles_ok: int = 0

    def observe(self, report: dict[str, Any]) -> None:
        for ex in report.get("executed", []):
            if ex["kind"] == "buy" and ex["status"] == "confirmed":
                self.buys_confirmed += 1
            elif ex["kind"] == "offer" and ex["status"] == "open":
                self.offers_opened += 1
        for b in report.get("bumped", []):
            if "bump_count" in b:
                self.offers_bumped += 1
        for c in report.get("cancelled", []):
            if c.get("status") == "cancelled":
                self.offers_cancelled += 1
        for r in report.get("relisted", []):
            if r.get("ok"):
                self.relisted += 1
        for m in report.get("marked_down", []):
            if m.get("status") == "confirmed":
                self.markdowns += 1
        for a in report.get("offers_accepted", []):
            if a.get("status") == "confirmed":
                self.offers_accepted += 1
        osync = report.get("ownership_sync") or {}
        for s in osync.get("sold", []):
            if "error" not in s:
                self.sold_detected += 1
        ssync = report.get("status_sync") or {}
        self.offer_fill_confirmed += int(ssync.get("confirmed", 0) or 0)
        risk = report.get("risk") or {}
        self.risk_blocked += int((risk.get("cycle") or {}).get("blocked", 0) or 0)
        for p in report.get("offer_pricing") or []:
            if p.get("status") == "repriced":
                self.dynamic_reprice += 1
            elif p.get("status") == "skipped":
                self.reprice_skipped += 1
        recheck = report.get("market_recheck") or {}
        if recheck.get("raised"):
            self.market_recheck_raised += 1
        if report.get("escalated"):
            self.escalation += 1
        if report.get("acquisition_paused"):
            self.acquisition_paused += 1


# --------------------------------------------------------------------------- #
# Simulated exchange — the single authoritative on-chain state
# --------------------------------------------------------------------------- #
class SimExchange:
    """Authoritative market + wallet state, evolving one hour per tick.

    Writes are staged by the trading-client method and only **committed** when
    their transaction is broadcast, exactly like the real prepare/sign/broadcast
    flow. Each tick advances the SOL rate, settles scheduled offer fills / sales
    / incoming bids, refreshes anomaly flags and replenishes the market.
    """

    def __init__(self) -> None:
        self.rng = Random(SEED)
        self.now: float = float(BASE_EPOCH)
        self.hour: int = 0
        self.sol_rate: float = 150.0

        self.market: dict[str, dict[str, Any]] = {}   # listed by others
        self.owned: dict[str, dict[str, Any]] = {}     # cards we hold
        self.meta: dict[str, dict[str, Any]] = {}      # per-nft sim bookkeeping
        self.our_offers: dict[str, dict[str, Any]] = {}   # nft -> {price, placed_at}
        self.competitor: dict[str, list] = {}          # nft -> [(wallet, amt, t)]
        self.incoming: dict[str, list] = {}            # nft -> [(wallet, amt, t)]
        self.listing_status: dict[str, dict[str, Any]] = {}  # filled-offer signals
        self.pending: dict[str, tuple] = {}            # staged, uncommitted writes

        self.usdc: float = 6000.0
        self.sol: float = 25.0
        self._sig = 0
        self._id = 0
        self._disp_idx = 0

        # anomaly flags (refreshed each tick)
        self.empty_market_now = False
        self.rate_limit_now = False
        self.broadcast_fail_now = False
        self.low_balance_now = False

        # observability counters
        self.broadcast_errors = 0
        self.empty_market_cycles = 0
        self.rate_limit_raises = 0
        self.offers_filled = 0
        self.external_sales = 0
        self.min_usdc = self.usdc

        self._seed_market()

    # ---- identity helpers -------------------------------------------- #
    def _next_sig(self) -> str:
        self._sig += 1
        return f"SIG{self._sig:08d}{'z' * 40}"

    def _spend(self, amount: float) -> None:
        self.usdc = max(0.0, self.usdc - max(0.0, amount))
        self.min_usdc = min(self.min_usdc, self.usdc)

    def _credit(self, amount: float) -> None:
        self.usdc += max(0.0, amount)

    # ---- market construction ----------------------------------------- #
    def _add_card(self, kind: str) -> str:
        self._id += 1
        nft = f"NFT{self._id:05d}"
        card_id = f"CARD{self._id:05d}"
        insured = float(self.rng.randint(40, 95))
        if kind == "buy":
            ask = round(insured * 0.70, 2)   # 30% discount -> direct buy
        else:
            ask = round(insured * 0.90, 2)   # 10% discount -> offer-only
        name = f"2023 #{self._id:03d} SimMon PSA 9 Svi EN Pokemon"
        self.market[nft] = _card_payload(
            nft=nft, card_id=card_id, name=name, insured=insured,
            oracle=insured, list_price=ask, owner_wallet=SELLER_WALLET,
            listed_epoch=self.now - 3600.0)
        self.meta[nft] = {
            "kind": kind, "card_id": card_id, "name": name, "insured": insured,
            "ask": ask, "disposition": None, "listed_epoch": None,
            "sold": False, "filled": False, "competitor_added": False,
            "incoming_added": False,
        }
        if kind == "contested":
            # A standing rival bid above our ceiling -> dynamic reprice skips it.
            self.competitor[nft] = [(COMPETITOR_WALLET, round(ask * 0.97, 2),
                                     self.now)]
        return nft

    def _seed_market(self) -> None:
        for _ in range(6):
            self._add_card("buy")
        for _ in range(4):
            self._add_card("offer_churn")
        for _ in range(2):
            self._add_card("offer_fill")
        for _ in range(3):
            self._add_card("contested")

    def _fresh_count(self, *kinds: str, exclude_offered: bool = True) -> int:
        n = 0
        for nft, info in self.meta.items():
            if info["kind"] not in kinds:
                continue
            if nft not in self.market:
                continue
            if exclude_offered and nft in self.our_offers:
                continue
            n += 1
        return n

    def _replenish(self) -> None:
        while self._fresh_count("buy") < 3:
            self._add_card("buy")
        while self._fresh_count("offer_churn") < 2:
            self._add_card("offer_churn")
        while self._fresh_count("offer_fill") < 1:
            self._add_card("offer_fill")
        while self._fresh_count("contested") < 2:
            self._add_card("contested")

    # ---- per-hour evolution ------------------------------------------ #
    def tick(self, hour: int) -> None:
        self.hour = hour
        self.now = float(BASE_EPOCH + hour * 3600)
        self.empty_market_now = (hour == EMPTY_MARKET_HOUR)
        self.rate_limit_now = (hour == RATE_LIMIT_HOUR)
        self.broadcast_fail_now = hour in BROADCAST_FAIL_HOURS
        self.low_balance_now = hour in LOW_BALANCE_HOURS

        # SOL price random walk (cards are USDC-priced, so this only exercises
        # the rate plumbing).
        self.sol_rate = max(120.0, min(180.0,
                                       self.sol_rate + self.rng.uniform(-3, 3)))

        # Settle / drive resting offers.
        for nft in list(self.our_offers):
            placed = self.our_offers[nft]["placed_at"]
            age = self.now - placed
            info = self.meta.get(nft, {})
            if info.get("kind") == "offer_fill" and not info.get("filled") \
                    and age >= 5 * 3600:
                self._fill_offer(nft)
            elif info.get("kind") != "offer_fill" \
                    and not info.get("competitor_added") and age >= 12 * 3600:
                # A rival appears above our bid -> the bump pass will react,
                # exhaust its bumps and finally cancel.
                self.competitor[nft] = [(COMPETITOR_WALLET,
                                         round(self.our_offers[nft]["price"]
                                               + 2.0, 2), self.now)]
                info["competitor_added"] = True

        # Drive the post-buy holding lifecycle off each card's listed age.
        for nft, info in list(self.meta.items()):
            le = info.get("listed_epoch")
            if le is None or info.get("sold") or nft not in self.owned:
                continue
            age = self.now - le
            disp = info.get("disposition")
            if disp == "sell_external" and age >= 40 * 3600:
                self._external_sale(nft)
            elif disp == "accept_offer" and not info.get("incoming_added") \
                    and age >= 58 * 3600:
                self.incoming[nft] = [(BUYER_WALLET,
                                       round(info["insured"] * 0.60, 2),
                                       self.now)]
                info["incoming_added"] = True

        self._replenish()

    def _fill_offer(self, nft: str) -> None:
        """A resting offer is accepted onto the book -> we acquire the card."""
        info = self.meta[nft]
        info["filled"] = True
        # Authoritative fill signal the status syncer will read.
        self.listing_status[nft] = {"exists": True, "marketplace": "CC",
                                    "status": "accepted"}
        self.market.pop(nft, None)
        self.competitor.pop(nft, None)
        price = self.our_offers.get(nft, {}).get("price", info["ask"])
        self.our_offers.pop(nft, None)
        # The card is now ours (held, unlisted). Escrow already funded it.
        self.owned[nft] = _card_payload(
            nft=nft, card_id=info["card_id"], name=info["name"],
            insured=info["insured"], oracle=info["insured"], list_price=None,
            owner_wallet=OUR_WALLET, listed_epoch=self.now)
        info["disposition"] = "hold"
        info["acquired_price"] = price
        self.offers_filled += 1

    def _external_sale(self, nft: str) -> None:
        """A listed holding sells to a third party -> it leaves our wallet."""
        card = self.owned.pop(nft, None)
        self.meta[nft]["sold"] = True
        self.incoming.pop(nft, None)
        if card and card.get("listing"):
            self._credit(float(card["listing"]["price"]))
        self.external_sales += 1

    # ---- commit-at-broadcast bookkeeping ----------------------------- #
    def _on_buy_commit(self, nft: str, price: float) -> None:
        info = self.meta.get(nft)
        if info is None:
            return
        self.market.pop(nft, None)
        self._spend(price)
        info["acquired_price"] = price
        disp = _DISPOSITIONS[self._disp_idx % len(_DISPOSITIONS)]
        self._disp_idx += 1
        info["disposition"] = disp
        oracle = info["insured"] * (1.30 if disp == "pump" else 1.0)
        self.owned[nft] = _card_payload(
            nft=nft, card_id=info["card_id"], name=info["name"],
            insured=info["insured"], oracle=oracle, list_price=None,
            owner_wallet=OUR_WALLET, listed_epoch=self.now)

    def _on_relist_commit(self, nft: str, price: float) -> None:
        card = self.owned.get(nft)
        info = self.meta.get(nft)
        if card is None or info is None:
            return
        iso = _iso(self.now)
        card["listing"] = {
            "id": f"{info['card_id']}-lst", "cardId": info["card_id"],
            "createdAt": iso, "currency": "USDC",
            "price": str(round(float(price), 2)),
            "receiptId": f"v2_{info['card_id']}", "marketplace": "CC",
            "sellerId": OUR_WALLET, "updatedAt": iso}
        card["listings"] = [card["listing"]]
        card["listedAt"] = iso
        info["listed_epoch"] = self.now

    def _on_markdown_commit(self, nft: str, price: float) -> None:
        card = self.owned.get(nft)
        if card is None or not card.get("listing"):
            return
        card["listing"]["price"] = str(round(float(price), 2))
        card["listings"] = [card["listing"]]

    def _on_accept_commit(self, nft: str, price: float) -> None:
        self.owned.pop(nft, None)
        self.incoming.pop(nft, None)
        if nft in self.meta:
            self.meta[nft]["sold"] = True
        self._credit(price)

    def _on_offer_commit(self, nft: str, price: float) -> None:
        self.our_offers[nft] = {"price": float(price), "placed_at": self.now}
        self._spend(price)   # escrow lock

    def _on_bump_commit(self, nft: str, price: float) -> None:
        cur = self.our_offers.get(nft)
        if cur is None:
            return
        self._spend(max(0.0, float(price) - float(cur["price"])))
        cur["price"] = float(price)

    def _on_cancel_commit(self, nft: str) -> None:
        cur = self.our_offers.pop(nft, None)
        self.competitor.pop(nft, None)
        if cur is not None:
            self._credit(float(cur["price"]))  # escrow refund

    # ---- card-activity feed (verified bare-array shape) -------------- #
    def card_activity(self, nft: str) -> list[dict[str, Any]]:
        info = self.meta.get(nft, {})
        card_id = info.get("card_id", "")
        rows: list[tuple[float, dict[str, Any]]] = []
        for (wallet, amount, epoch) in self.competitor.get(nft, []):
            rows.append((epoch, _offer_event(nft=nft, card_id=card_id,
                                             wallet=wallet, amount=amount,
                                             epoch=epoch)))
        ours = self.our_offers.get(nft)
        if ours is not None:
            rows.append((ours["placed_at"],
                         _offer_event(nft=nft, card_id=card_id,
                                      wallet=OUR_WALLET, amount=ours["price"],
                                      epoch=ours["placed_at"])))
        for (wallet, amount, epoch) in self.incoming.get(nft, []):
            rows.append((epoch, _offer_event(nft=nft, card_id=card_id,
                                             wallet=wallet, amount=amount,
                                             epoch=epoch)))
        rows.sort(key=lambda r: r[0], reverse=True)  # newest first
        return [event for _, event in rows]


# --------------------------------------------------------------------------- #
# Source client (marketplace sourcing + SOL rate)
# --------------------------------------------------------------------------- #
class SimSourceClient:
    """Stands in for :class:`CCClient` (the sourcing read path)."""

    def __init__(self, exch: SimExchange) -> None:
        self._exch = exch

    def fetch_sol_usd(self) -> float:
        return self._exch.sol_rate

    def fetch_marketplace_page_with_retry(self, page: int,
                                          step: int) -> dict[str, Any]:
        ex = self._exch
        if ex.rate_limit_now:
            ex.rate_limit_raises += 1
            raise CCRateLimitError("simulated rate limit (429)")
        if ex.empty_market_now:
            ex.empty_market_cycles += 1
            return {"filterNFtCard": [], "totalPages": 1, "totalCards": 0}
        if page > 1:
            return {"filterNFtCard": [], "totalPages": 1}
        cards = list(ex.market.values())
        return {"filterNFtCard": cards, "totalPages": 1,
                "totalCards": len(cards)}


# --------------------------------------------------------------------------- #
# Trading client (authenticated write + read path) — one per cycle
# --------------------------------------------------------------------------- #
class SimTradingClient:
    """Stands in for :class:`CCTradingClient`; delegates to the exchange.

    The engine builds a fresh trading client inside every maintenance pass, so
    all instances must share the same authoritative exchange (injected here).
    Write methods *stage* their effect and return a bare base64 transaction
    (``{"data": ...}``); ``broadcast`` commits the staged effect.
    """

    def __init__(self, exch: SimExchange) -> None:
        self._exch = exch

    # -- writes (return a wrapped base64 tx) --------------------------- #
    def _tx(self, label: str, nft: str, op: tuple) -> dict[str, Any]:
        self._exch.pending[nft] = op
        return {"data": f"BASE64TX::{label}::{nft}"}

    def initiate_buy(self, *, nft, price, wallet, currency="USDC", **_):
        return self._tx("buy", nft, ("buy", {"nft": nft, "price": price}))

    def make_offer(self, *, nft, card_id="", price, wallet="", currency="USDC",
                   **_):
        return self._tx("offer", nft, ("offer", {"nft": nft, "price": price}))

    def update_offer(self, *, nft, price, wallet="", currency="USDC", **_):
        return self._tx("bump", nft, ("bump", {"nft": nft, "price": price}))

    def cancel_offer(self, *, nft="", wallet="", currency="USDC",
                     keep_in_escrow=False, **_):
        return self._tx("cancel", nft, ("cancel", {"nft": nft}))

    def create_listing(self, *, nft, card_id, price, wallet, currency="USDC",
                       **_):
        return self._tx("list", nft, ("list", {"nft": nft, "price": price}))

    def update_listing(self, *, nft, price, wallet, currency="USDC", **_):
        return self._tx("markdown", nft,
                        ("markdown", {"nft": nft, "price": price}))

    def accept_offer(self, *, nft, buyer, price, wallet, currency="USDC", **_):
        return self._tx("accept", nft,
                        ("accept", {"nft": nft, "price": price}))

    def cancel_listing(self, *, nft, wallet, currency="USDC", **_):
        return self._tx("cancel_listing", nft, ("cancel_listing", {"nft": nft}))

    def broadcast(self, *, signed_tx, wallet="", nft="", extra=None):
        ex = self._exch
        op = ex.pending.pop(nft, None)
        sig = ex._next_sig()
        if op is None:
            return {"success": True, "signature": sig,
                    "message": "Transaction broadcast successfully"}
        kind, payload = op
        if ex.broadcast_fail_now and kind in ("buy", "offer"):
            ex.broadcast_errors += 1
            raise CCServerError("simulated broadcast failure (500)")
        if kind == "buy":
            ex._on_buy_commit(nft, payload["price"])
        elif kind == "offer":
            ex._on_offer_commit(nft, payload["price"])
        elif kind == "bump":
            ex._on_bump_commit(nft, payload["price"])
        elif kind == "cancel":
            ex._on_cancel_commit(nft)
        elif kind == "list":
            ex._on_relist_commit(nft, payload["price"])
        elif kind == "markdown":
            ex._on_markdown_commit(nft, payload["price"])
        elif kind == "accept":
            ex._on_accept_commit(nft, payload["price"])
        elif kind == "cancel_listing":
            card = ex.owned.get(nft)
            if card is not None:
                card["listing"] = None
                card["listings"] = []
        return {"success": True, "signature": sig,
                "message": "Transaction broadcast successfully"}

    # -- reads --------------------------------------------------------- #
    def check_listing_status(self, *, nft, wallet):
        ex = self._exch
        if nft in ex.listing_status:
            return ex.listing_status[nft]
        return {"exists": True, "marketplace": "CC", "listing": {}}

    def get_owned_cards(self, *, wallet, page=1, step=96, order_by="dateDesc"):
        cards = list(self._exch.owned.values()) if page == 1 else []
        return {"filterNFtCard": cards, "totalPages": 1,
                "totalCards": len(cards)}

    def get_card_activity(self, *, nft, day=60):
        return {"data": self._exch.card_activity(nft)}

    def get_wallet_activity(self, *, wallet, day=None):
        # The bot maintains its own durable state across the run, so the
        # recovery/backfill feed is intentionally empty here.
        return {"data": []}


# --------------------------------------------------------------------------- #
# Wallet
# --------------------------------------------------------------------------- #
class SimWallet:
    """Armed signing wallet whose balances track the exchange."""

    def __init__(self, exch: SimExchange) -> None:
        self._exch = exch
        self.can_sign = True
        self.address = OUR_WALLET
        self.signed: list[str] = []

    def sign_transaction(self, serialized_tx: str) -> str:
        self.signed.append(serialized_tx)
        return f"SIGNED::{serialized_tx}"

    def sol_balance(self) -> float:
        return self._exch.sol

    def usdc_balance(self) -> float:
        # A deliberate low-balance window trips the min-operate pause.
        if self._exch.low_balance_now:
            return 10.0
        return self._exch.usdc


# --------------------------------------------------------------------------- #
# Session provider (armed, static token)
# --------------------------------------------------------------------------- #
class SimSessionProvider:
    """Returns a valid static auth session (no network)."""

    def __init__(self, *, token: str = "tok", account_id: str = "acct") -> None:
        self._token = token
        self._account_id = account_id
        self.invalidated = 0
        self.get_calls = 0

    def get_session(self) -> AuthSession:
        self.get_calls += 1
        return AuthSession(token=self._token, account_id=self._account_id)

    def invalidate(self) -> None:
        self.invalidated += 1


# --------------------------------------------------------------------------- #
# Config for the run
# --------------------------------------------------------------------------- #
# The full set of TraderConfig defaults (mirrors tests/conftest._CONFIG_DEFAULTS)
# so this script is self-contained and never imports pytest.
_CONFIG_DEFAULTS: dict[str, Any] = dict(
    rpc_url="https://rpc.test.invalid",
    wallet_address="",
    wallet_secret="",
    live=False,
    auth_provider="none",
    privy_app_id="",
    privy_client_id="",
    cc_token="",
    reserve_usdc=0.0,
    gas_reserve_sol=0.0,
    base_max_card_usd=40.0,
    min_card_usd=0.0,
    min_discount_pct=20.0,
    direct_buy_pct=50.0,
    offer_pct=50.0,
    offer_discount_pct=10.0,
    offer_open_discount_pct=0.0,
    offer_ceiling_pct=0.0,
    offer_increment_usd=0.01,
    offer_max_premium_pct=0.0,
    resell_discount_pct=10.0,
    escalation_volume_usd=1000.0,
    escalation_max_card_usd=100.0,
    max_spend_per_cycle_usd=0.0,
    max_spend_per_day_usd=0.0,
    max_open_positions=0,
    max_consecutive_failures=0,
    offer_bump_usd=0.10,
    offer_bump_age_hours=24.0,
    offer_bump_max=3,
    min_operate_usd=0.0,
    max_owned_cards=0,
    unpopular_days=7.0,
    markdown_delay_days=3.0,
    markdown_step_pct=1.0,
    markdown_interval_days=3.0,
    markdown_jitter_pct=0.0,
    markdown_min_change_usd=0.0,
    offer_accept_delay_days=3.0,
    offer_accept_min_market_pct=0.0,
    market_recheck_hours=24.0,
    categories=("Pokemon",),
    max_pages=5,
    allowed_marketplaces=("CC",),
    loop_interval_sec=60.0,
    ledger_path="",
    log_path="",
    auto_resume=False,
)


# Fields that must be tuples on TraderConfig (JSON arrays decode to lists).
_TUPLE_FIELDS = ("categories", "allowed_marketplaces")

# Wiring the script always forces (not meant to be tuned via the settings file).
_WIRING_OVERRIDES: dict[str, Any] = dict(
    live=True,
    auth_provider="static",
    cc_token="tok",
    wallet_address=OUR_WALLET,
)


def _load_settings() -> dict[str, Any]:
    """Load the tunable trader settings from ``simulation/trade_setting.json``.

    Keys prefixed with ``_`` (e.g. ``_comment``) and any key that is not a real
    :class:`TraderConfig` field are ignored. JSON arrays for tuple-typed fields
    are converted to tuples. Returns an empty dict when the file is missing so
    the built-in defaults are used.
    """
    if not os.path.exists(SETTINGS_PATH):
        return {}
    with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    settings: dict[str, Any] = {}
    for key, value in raw.items():
        if key.startswith("_") or key not in _CONFIG_DEFAULTS:
            continue
        if key in _TUPLE_FIELDS and isinstance(value, list):
            value = tuple(value)
        settings[key] = value
    return settings


def _sim_config() -> TraderConfig:
    """Build the run config: defaults <- ``trade_setting.json`` <- wiring."""
    settings = _load_settings()
    return TraderConfig(
        **{**_CONFIG_DEFAULTS, **settings, **_WIRING_OVERRIDES})


# --------------------------------------------------------------------------- #
# Compressed-clock environment (replaces pytest's monkeypatch)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _sim_environment(exch: SimExchange):
    """Patch the global clock + trading client, restoring everything after.

    Three things must point at the simulated clock/exchange:

    1. ``time.time`` -> ``exch.now`` (engine, reconcile and store read it);
    2. ``engine.CCTradingClient`` -> a fresh :class:`SimTradingClient` per pass;
    3. ``Order.created_at``/``updated_at`` default factories. These are
       ``field(default_factory=time.time)`` whose builtin ``time.time`` was
       baked into the dataclass ``__init__`` closure at import time, so patching
       the ``time`` module alone never reaches them; the specific closure cells
       (writable since CPython 3.7) are redirected too, otherwise orders are
       stamped on real wall-clock time, never age, and offers never bump.
    """
    from collectorcrypt.trader import engine as engine_mod

    orig_time = time.time
    orig_client = engine_mod.CCTradingClient
    sim_clock = lambda: exch.now  # noqa: E731 - tiny inline factory

    init = orders_mod.Order.__init__
    targets = {"__dataclass_dflt_created_at__", "__dataclass_dflt_updated_at__"}
    cells = init.__closure__ or ()
    saved_cells = [
        cell for name, cell in zip(init.__code__.co_freevars, cells)
        if name in targets
    ]
    if len(saved_cells) != len(targets):
        raise RuntimeError(
            "could not locate Order time-stamp factory cells "
            f"(found {len(saved_cells)}/{len(targets)}); dataclass internals "
            "changed")
    saved_values = [cell.cell_contents for cell in saved_cells]

    try:
        time.time = sim_clock
        engine_mod.CCTradingClient = lambda **kwargs: SimTradingClient(exch)
        for cell in saved_cells:
            cell.cell_contents = sim_clock
        yield
    finally:
        time.time = orig_time
        engine_mod.CCTradingClient = orig_client
        for cell, value in zip(saved_cells, saved_values):
            cell.cell_contents = value


# --------------------------------------------------------------------------- #
# Run log
# --------------------------------------------------------------------------- #
class RunLog:
    """Accumulates the human-readable run log, printed and written to disk."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def line(self, text: str = "") -> None:
        print(text)
        self._lines.append(text)

    def rule(self, char: str = "-") -> None:
        self.line(char * 72)

    def header(self, title: str) -> None:
        self.rule("=")
        self.line(f" {title}")
        self.rule("=")

    def section(self, title: str) -> None:
        self.line("")
        self.rule("-")
        self.line(f" {title}")
        self.rule("-")

    def write(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self._lines) + "\n")


def _when(hour: int) -> str:
    """Render an hour index as ``D<day> HH:00 (h###)``."""
    day = hour // 24 + 1
    hod = hour % 24
    return f"D{day} {hod:02d}:00 (h{hour:03d})"


# Per-cycle event extraction -------------------------------------------------- #
_COUNT_KEYS = (
    "buys", "offers_opened", "bumped", "cancelled", "relisted", "markdowns",
    "sold", "accepted", "fills", "reprice_skipped", "recheck_raised",
    "risk_blocked", "escalated", "paused",
)


def _collect(report: dict[str, Any], exch: SimExchange,
             hour: int) -> tuple[list[str], dict[str, int], list[dict[str, Any]]]:
    """Return (human event lines, per-cycle counts, structured rows).

    The structured rows mirror the human log lines but in a flat, machine
    readable shape for the per-event CSV (``type``/``name``/``nft``/prices/
    ``detail``).
    """
    events: list[str] = []
    counts = {k: 0 for k in _COUNT_KEYS}
    rows: list[dict[str, Any]] = []

    def _row(kind: str, *, name: str = "", nft: str = "",
             price_usd: float = 0.0, market_usd: float = 0.0,
             resell_usd: float = 0.0, detail: str = "") -> None:
        rows.append({
            "type": kind,
            "name": name,
            "nft": nft,
            "price_usd": round(float(price_usd), 2),
            "market_usd": round(float(market_usd), 2),
            "resell_usd": round(float(resell_usd), 2),
            "detail": detail,
        })

    # Scheduled anomalies active this hour.
    if exch.empty_market_now:
        events.append("[!] anomaly: marketplace returned an EMPTY page")
        _row("anomaly", detail="empty marketplace page")
    if exch.broadcast_fail_now:
        events.append("[!] anomaly: broadcast 500 window active "
                      "(buy/offer writes fail this hour)")
        _row("anomaly", detail="broadcast 500 window")
    if exch.low_balance_now:
        events.append("[!] anomaly: low-balance window (wallet shows $10)")
        _row("anomaly", detail="low-balance window")

    if report.get("acquisition_paused"):
        counts["paused"] = 1
        reason = report.get('pause_reason', 'min-operate floor')
        events.append(f"PAUSE  acquisition paused -> {reason}")
        _row("pause", detail=reason)

    if report.get("escalated"):
        counts["escalated"] = 1
        cap = report.get("card_cap_usd")
        cap_txt = f" (per-card cap ${cap:.2f})" if isinstance(cap, (int, float)) else ""
        events.append(f"ESCALATION engaged{cap_txt}")
        _row("escalation", price_usd=cap if isinstance(cap, (int, float)) else 0.0,
             detail="per-card cap raised")

    for ex in report.get("executed", []):
        if ex.get("kind") == "buy" and ex.get("status") == "confirmed":
            counts["buys"] += 1
            events.append(
                f"BUY    {ex.get('name', '?')}  cost ${ex.get('price_usd', 0):.2f}"
                f"  (market ${ex.get('market_usd', 0):.2f}, "
                f"resell ${ex.get('resell_usd', 0):.2f})")
            _row("buy", name=ex.get("name", "?"), nft=ex.get("nft", ""),
                 price_usd=ex.get("price_usd", 0.0),
                 market_usd=ex.get("market_usd", 0.0),
                 resell_usd=ex.get("resell_usd", 0.0))
        elif ex.get("kind") == "offer" and ex.get("status") == "open":
            counts["offers_opened"] += 1
            events.append(
                f"OFFER  {ex.get('name', '?')}  bid ${ex.get('price_usd', 0):.2f}")
            _row("offer", name=ex.get("name", "?"), nft=ex.get("nft", ""),
                 price_usd=ex.get("price_usd", 0.0),
                 market_usd=ex.get("market_usd", 0.0),
                 resell_usd=ex.get("resell_usd", 0.0))

    for p in report.get("offer_pricing") or []:
        if p.get("status") == "skipped":
            counts["reprice_skipped"] += 1
            detail = p.get("detail", "no winnable price")
            events.append(
                f"SKIP   {p.get('name', '?')}  contested/unwinnable ({detail})")
            _row("skip", name=p.get("name", "?"), nft=p.get("nft", ""),
                 detail=detail)

    ssync = report.get("status_sync") or {}
    confirmed = int(ssync.get("confirmed", 0) or 0)
    if confirmed:
        counts["fills"] += confirmed
        events.append(f"FILL   {confirmed} resting offer(s) confirmed -> acquired")
        _row("fill", price_usd=0.0, detail=f"{confirmed} resting offer(s) confirmed")

    osync = report.get("ownership_sync") or {}
    for s in osync.get("sold", []):
        if "error" not in s:
            counts["sold"] += 1
            events.append(f"SOLD   {s.get('name', s.get('nft', '?'))}  "
                          f"(left wallet / external sale)")
            _row("sold", name=s.get("name", s.get("nft", "?")),
                 nft=s.get("nft", ""), detail="left wallet / external sale")

    for b in report.get("bumped", []):
        if "bump_count" in b:
            counts["bumped"] += 1
            events.append(
                f"BUMP   {b.get('name', '?')}  -> ${b.get('new_price_usd', 0):.2f}"
                f"  (bump #{b.get('bump_count')})")
            _row("bump", name=b.get("name", "?"), nft=b.get("nft", ""),
                 price_usd=b.get("new_price_usd", 0.0),
                 detail=f"bump #{b.get('bump_count')}")

    for c in report.get("cancelled", []):
        if c.get("status") == "cancelled":
            counts["cancelled"] += 1
            events.append(f"CANCEL {c.get('name', '?')}  offer cancelled "
                          f"(escrow refunded)")
            _row("cancel", name=c.get("name", "?"), nft=c.get("nft", ""),
                 detail="escrow refunded")

    for r in report.get("relisted", []):
        if r.get("ok"):
            counts["relisted"] += 1
            events.append(f"LIST   {r.get('name', '?')}  relisted @ "
                          f"${r.get('price_usd', 0):.2f}")
            _row("relist", name=r.get("name", "?"), nft=r.get("nft", ""),
                 price_usd=r.get("price_usd", 0.0),
                 market_usd=r.get("market_usd", 0.0))

    for m in report.get("marked_down", []):
        if m.get("status") == "confirmed":
            counts["markdowns"] += 1
            events.append(
                f"MARK   {m.get('name', '?')}  ${m.get('old_price_usd', 0):.2f}"
                f" -> ${m.get('new_price_usd', 0):.2f}")
            _row("markdown", name=m.get("name", "?"), nft=m.get("nft", ""),
                 price_usd=m.get("new_price_usd", 0.0),
                 detail=f"from ${m.get('old_price_usd', 0):.2f}")

    for a in report.get("offers_accepted", []):
        if a.get("status") == "confirmed":
            counts["accepted"] += 1
            events.append(
                f"ACCEPT {a.get('name', '?')}  took bid "
                f"${a.get('offer_usd', 0):.2f} from {_short(a.get('buyer', '?'))}")
            _row("accept", name=a.get("name", "?"), nft=a.get("nft", ""),
                 price_usd=a.get("offer_usd", 0.0),
                 detail=f"buyer {_short(a.get('buyer', '?'))}")

    recheck = report.get("market_recheck") or {}
    for rr in recheck.get("raised", []):
        counts["recheck_raised"] += 1
        events.append(
            f"RECHK  {rr.get('name', '?')}  market rose -> relist target "
            f"${rr.get('new_list_price_usd', 0):.2f}")
        _row("recheck", name=rr.get("name", "?"), nft=rr.get("nft", ""),
             price_usd=rr.get("new_list_price_usd", 0.0),
             market_usd=rr.get("new_market_usd", 0.0),
             detail="market rose")

    risk = report.get("risk") or {}
    blocked = int((risk.get("cycle") or {}).get("blocked", 0) or 0)
    if blocked:
        counts["risk_blocked"] += blocked
        events.append(f"RISK   {blocked} planned order(s) blocked by the risk gate")
        _row("risk_block", price_usd=0.0, detail=f"{blocked} order(s) blocked")

    return events, counts, rows


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run_simulation(log: RunLog) -> tuple[bool, dict[str, Any]]:
    """Drive the full week and emit the log.

    Returns ``(all_ok, data)`` where ``data`` holds the structured rows for the
    CSV export (``cycles``/``events``/``daily``/``summary``/``checks``).
    """
    exch = SimExchange()
    store_dir = tempfile.mkdtemp(prefix="cc_sim_store_")
    store = OrderStore(os.path.join(store_dir, "trader_store.db"))
    start_usdc = exch.usdc
    start_sol = exch.sol

    cov = Coverage()
    totals = {k: 0 for k in _COUNT_KEYS}
    day_totals: dict[int, dict[str, int]] = {
        d: {k: 0 for k in _COUNT_KEYS} for d in range(1, 8)
    }
    sourcing_aborted = 0
    quiet_cycles = 0

    # Structured rows for the CSV export.
    cycle_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []

    log.header("CollectorCrypt - 7-Day Live Trading Simulation")
    log.line(f" Start (sim clock) : {_iso(float(BASE_EPOCH))}")
    log.line(f" Wallet            : {OUR_WALLET}")
    log.line(f" Mode              : LIVE (fully armed)")
    log.line(f" Duration          : {HOURS} cycles (7 days, 1 cycle/hour, compressed)")
    log.line(f" RNG seed          : {SEED}")
    log.line(f" Start USDC / SOL  : ${exch.usdc:.2f} / {exch.sol:.2f} SOL")
    log.line(f" Scheduled anomalies:")
    log.line(f"   - empty marketplace page  @ h{EMPTY_MARKET_HOUR}")
    log.line(f"   - sourcing rate-limit     @ h{RATE_LIMIT_HOUR}")
    log.line(f"   - broadcast 500 window    @ h{sorted(BROADCAST_FAIL_HOURS)}")
    log.line(f"   - low-balance pause window @ h{sorted(LOW_BALANCE_HOURS)}")

    log.section("CHRONOLOGICAL EVENT LOG")

    try:
        with _sim_environment(exch):
            cfg = _sim_config()
            engine = TradeEngine(
                cfg,
                client=SimSourceClient(exch),
                wallet=SimWallet(exch),
                store=store,
                session_provider=SimSessionProvider(),
            )

            for hour in range(HOURS):
                exch.tick(hour)
                day = hour // 24 + 1
                iso_time = _iso(exch.now)
                try:
                    report = engine.run_cycle()
                except CCRateLimitError:
                    # Sourcing has no internal try/except, so a rate limit
                    # propagates; the real manager swallows it and continues.
                    sourcing_aborted += 1
                    log.line(f"[{_when(hour)}]")
                    log.line("  [!] anomaly: sourcing hit a 429 rate-limit -> "
                             "cycle aborted (resumes next hour)")
                    cycle_rows.append({
                        "hour": hour, "day": day, "iso_time": iso_time,
                        "usdc": round(exch.usdc, 2), "sol": round(exch.sol, 2),
                        "held": len(exch.owned),
                        "open_offers": len(exch.our_offers),
                        "rate_limited": 1, "empty_market": 0,
                        "broadcast_fail": 0, "low_balance": 0,
                        **{k: 0 for k in _COUNT_KEYS},
                    })
                    event_rows.append({
                        "hour": hour, "day": day, "iso_time": iso_time,
                        "type": "rate_limit_abort", "name": "", "nft": "",
                        "price_usd": 0.0, "market_usd": 0.0, "resell_usd": 0.0,
                        "detail": "sourcing 429 -> cycle aborted",
                    })
                    continue

                if report.get("mode") != "LIVE" or report.get("wallet") != OUR_WALLET:
                    raise RuntimeError(
                        f"cycle {hour} not armed-live: mode="
                        f"{report.get('mode')} wallet={report.get('wallet')}")
                if exch.usdc < 0.0:
                    raise RuntimeError(f"cycle {hour}: USDC went negative")

                cov.cycles_ok += 1
                cov.observe(report)
                events, counts, rows = _collect(report, exch, hour)
                for k in _COUNT_KEYS:
                    totals[k] += counts[k]
                    day_totals[day][k] += counts[k]

                cycle_rows.append({
                    "hour": hour, "day": day, "iso_time": iso_time,
                    "usdc": round(exch.usdc, 2), "sol": round(exch.sol, 2),
                    "held": len(exch.owned),
                    "open_offers": len(exch.our_offers),
                    "rate_limited": 0,
                    "empty_market": int(exch.empty_market_now),
                    "broadcast_fail": int(exch.broadcast_fail_now),
                    "low_balance": int(exch.low_balance_now),
                    **{k: counts[k] for k in _COUNT_KEYS},
                })
                for r in rows:
                    event_rows.append({
                        "hour": hour, "day": day, "iso_time": iso_time, **r})

                if events:
                    log.line(f"[{_when(hour)}]  "
                             f"USDC ${exch.usdc:.2f} | held {len(exch.owned)} "
                             f"| open offers {len(exch.our_offers)}")
                    for ev in events:
                        log.line(f"    {ev}")
                else:
                    quiet_cycles += 1
    finally:
        shutil.rmtree(store_dir, ignore_errors=True)

    # ---- per-day rollup ---------------------------------------------- #
    log.section("PER-DAY ROLLUP")
    headers = ["Day", "buys", "offers", "bumps", "cxl", "relist", "markd",
               "sold", "accept", "fills", "skip", "rechk", "riskX"]
    log.line(" " + "  ".join(f"{h:>6}" for h in headers))
    rollup_keys = ["buys", "offers_opened", "bumped", "cancelled", "relisted",
                   "markdowns", "sold", "accepted", "fills", "reprice_skipped",
                   "recheck_raised", "risk_blocked"]
    daily_rows: list[dict[str, Any]] = []
    for d in range(1, 8):
        row = [f"D{d}"] + [str(day_totals[d][k]) for k in rollup_keys]
        log.line(" " + "  ".join(f"{c:>6}" for c in row))
        daily_rows.append({"day": d, **{k: day_totals[d][k] for k in _COUNT_KEYS}})

    # ---- totals ------------------------------------------------------ #
    summary_rows: list[dict[str, Any]] = [
        {"metric": "start_usdc", "value": round(start_usdc, 2)},
        {"metric": "start_sol", "value": round(start_sol, 2)},
        {"metric": "cycles_executed_ok", "value": cov.cycles_ok},
        {"metric": "cycles_total", "value": HOURS},
        {"metric": "cycles_no_activity", "value": quiet_cycles},
        {"metric": "sourcing_aborted_429", "value": sourcing_aborted},
        {"metric": "direct_buys_confirmed", "value": cov.buys_confirmed},
        {"metric": "offers_opened", "value": cov.offers_opened},
        {"metric": "offers_bumped", "value": cov.offers_bumped},
        {"metric": "offers_cancelled", "value": cov.offers_cancelled},
        {"metric": "resting_offers_filled", "value": cov.offer_fill_confirmed},
        {"metric": "cards_relisted", "value": cov.relisted},
        {"metric": "listings_marked_down", "value": cov.markdowns},
        {"metric": "incoming_offers_accepted", "value": cov.offers_accepted},
        {"metric": "sales_detected", "value": cov.sold_detected},
        {"metric": "contested_offers_skipped", "value": cov.reprice_skipped},
        {"metric": "market_rechecks_raised", "value": cov.market_recheck_raised},
        {"metric": "escalation_cycles", "value": cov.escalation},
        {"metric": "acquisition_paused_cycles", "value": cov.acquisition_paused},
        {"metric": "risk_blocked_orders", "value": cov.risk_blocked},
        {"metric": "final_usdc", "value": round(exch.usdc, 2)},
        {"metric": "final_sol", "value": round(exch.sol, 2)},
        {"metric": "lowest_usdc", "value": round(exch.min_usdc, 2)},
        {"metric": "cards_still_held", "value": len(exch.owned)},
        {"metric": "open_offers_resting", "value": len(exch.our_offers)},
        {"metric": "external_sales_settled", "value": exch.external_sales},
        {"metric": "broadcast_500s_handled", "value": exch.broadcast_errors},
        {"metric": "rate_limit_raises_handled", "value": exch.rate_limit_raises},
        {"metric": "empty_market_pages_served", "value": exch.empty_market_cycles},
        {"metric": "final_sol_usd_rate", "value": round(exch.sol_rate, 2)},
    ]
    log.section("WEEK TOTALS")
    log.line(f" Cycles executed OK         : {cov.cycles_ok} / {HOURS}")
    log.line(f" Cycles with no activity    : {quiet_cycles}")
    log.line(f" Sourcing aborted (429)     : {sourcing_aborted}")
    log.line(f" Direct buys confirmed      : {cov.buys_confirmed}")
    log.line(f" Offers opened              : {cov.offers_opened}")
    log.line(f" Offers bumped              : {cov.offers_bumped}")
    log.line(f" Offers cancelled           : {cov.offers_cancelled}")
    log.line(f" Resting offers filled      : {cov.offer_fill_confirmed}")
    log.line(f" Cards relisted             : {cov.relisted}")
    log.line(f" Listings marked down       : {cov.markdowns}")
    log.line(f" Incoming offers accepted   : {cov.offers_accepted}")
    log.line(f" Sales detected (ownership) : {cov.sold_detected}")
    log.line(f" Contested offers skipped   : {cov.reprice_skipped}")
    log.line(f" Market re-checks raised     : {cov.market_recheck_raised}")
    log.line(f" Escalation cycles          : {cov.escalation}")
    log.line(f" Acquisition-paused cycles  : {cov.acquisition_paused}")
    log.line(f" Risk-gate blocked orders   : {cov.risk_blocked}")

    # ---- final exchange state ---------------------------------------- #
    log.section("FINAL EXCHANGE STATE")
    log.line(f" USDC balance               : ${exch.usdc:.2f}")
    log.line(f" SOL balance                : {exch.sol:.2f} SOL")
    log.line(f" Lowest USDC during run     : ${exch.min_usdc:.2f}")
    log.line(f" Cards still held           : {len(exch.owned)}")
    log.line(f" Open offers resting        : {len(exch.our_offers)}")
    log.line(f" Resting offers filled      : {exch.offers_filled}")
    log.line(f" External sales settled     : {exch.external_sales}")
    log.line(f" Broadcast 500s handled     : {exch.broadcast_errors}")
    log.line(f" Rate-limit raises handled  : {exch.rate_limit_raises}")
    log.line(f" Empty-market pages served  : {exch.empty_market_cycles}")
    log.line(f" Final SOL/USD rate         : {exch.sol_rate:.2f}")

    # ---- sanity checks ----------------------------------------------- #
    checks: list[tuple[str, bool]] = [
        ("rate-limit sourcing abort happened", sourcing_aborted >= 1),
        ("rate-limit raised at least once", exch.rate_limit_raises >= 1),
        ("empty-market anomaly served", exch.empty_market_cycles >= 1),
        ("broadcast 500 hit a write", exch.broadcast_errors >= 1),
        ("a resting offer was filled", exch.offers_filled >= 1),
        ("a holding sold externally", exch.external_sales >= 1),
        ("almost every cycle ran OK", cov.cycles_ok >= HOURS - 5),
        ("direct buys confirmed (>=5)", cov.buys_confirmed >= 5),
        ("offers opened (>=5)", cov.offers_opened >= 5),
        ("an offer was bumped", cov.offers_bumped >= 1),
        ("an exhausted offer was cancelled", cov.offers_cancelled >= 1),
        ("a card was relisted", cov.relisted >= 1),
        ("a listing was marked down", cov.markdowns >= 1),
        ("ownership sync detected a sale", cov.sold_detected >= 1),
        ("status sync confirmed a fill", cov.offer_fill_confirmed >= 1),
        ("an incoming offer was accepted", cov.offers_accepted >= 1),
        ("the risk gate blocked an order", cov.risk_blocked >= 1),
        ("a contested offer was skipped", cov.reprice_skipped >= 1),
        ("a market re-check raised price", cov.market_recheck_raised >= 1),
        ("the escalation cap engaged", cov.escalation >= 1),
        ("the min-operate pause engaged", cov.acquisition_paused >= 1),
        ("USDC never went negative", exch.min_usdc >= 0.0),
    ]
    log.section("SANITY CHECKS")
    all_ok = True
    checks_rows: list[dict[str, Any]] = []
    for label, ok in checks:
        all_ok = all_ok and ok
        log.line(f" [{'PASS' if ok else 'FAIL'}]  {label}")
        checks_rows.append({"check": label, "passed": int(bool(ok))})

    log.line("")
    log.rule("=")
    log.line(f" RESULT: {'ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'}"
             f"  ({sum(1 for _, ok in checks if ok)}/{len(checks)})")
    log.rule("=")

    data = {
        "cycles": cycle_rows,
        "events": event_rows,
        "daily": daily_rows,
        "summary": summary_rows,
        "checks": checks_rows,
    }
    return all_ok, data


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
_CSV_FIELDS: dict[str, list[str]] = {
    "cycles": (
        ["hour", "day", "iso_time", "usdc", "sol", "held", "open_offers",
         "rate_limited", "empty_market", "broadcast_fail", "low_balance"]
        + list(_COUNT_KEYS)
    ),
    "events": ["hour", "day", "iso_time", "type", "name", "nft",
               "price_usd", "market_usd", "resell_usd", "detail"],
    "daily": ["day"] + list(_COUNT_KEYS),
    "summary": ["metric", "value"],
    "checks": ["check", "passed"],
}


def _write_csvs(data: dict[str, Any], stamp: str, csv_dir: str) -> dict[str, str]:
    """Write each dataset to ``<csv_dir>/<name>_<stamp>.csv``.

    Returns a mapping of dataset name -> written path.
    """
    os.makedirs(csv_dir, exist_ok=True)
    written: dict[str, str] = {}
    for name, fields in _CSV_FIELDS.items():
        path = os.path.join(csv_dir, f"{name}_{stamp}.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for row in data.get(name, []):
                writer.writerow({k: row.get(k, "") for k in fields})
        written[name] = path
    return written


def main() -> int:
    log = RunLog()
    ok, data = run_simulation(log)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(RUNS_DIR, stamp)
    logs_dir = os.path.join(run_dir, "logs")
    csv_dir = os.path.join(run_dir, "csv")
    os.makedirs(logs_dir, exist_ok=True)

    log_path = os.path.join(logs_dir, f"sim_{stamp}.log")
    log.write(log_path)
    csv_paths = _write_csvs(data, stamp, csv_dir)

    # Snapshot the settings this run used next to its data so the dashboard can
    # show exactly what the run was configured with.
    if os.path.exists(SETTINGS_PATH):
        with contextlib.suppress(OSError):
            shutil.copyfile(SETTINGS_PATH,
                            os.path.join(run_dir, "trade_setting.json"))

    print("")
    print(f"Run folder: {run_dir}")
    print(f"Full log written to: {log_path}")
    print("CSV data written to:")
    for name, path in csv_paths.items():
        print(f"  - {name:<8} {path}")
    print("")
    print("Visualize it in your browser:")
    print("  .venv\\Scripts\\python.exe datavisualizing\\visualize.py")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
