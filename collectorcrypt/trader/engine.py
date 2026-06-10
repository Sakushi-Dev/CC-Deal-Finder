"""Trader orchestration.

Ties the pieces together for one decision cycle:

1. read SOL/USDC balance -> available volume (what the bot may still spend),
2. source CC listings via the existing :class:`CCClient` + normalizer,
3. build a quantity-first, CC-only :class:`BuyPlan` (with escalation),
4. execute it (dry-run by default; live only if explicitly enabled *and*
   the live executor is implemented).

Returns a plain ``dict`` report so it can be printed, logged or served as JSON.
"""
from __future__ import annotations

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Any

from ..api import CCClient
from ..normalize import normalize_card
from .audit import TransactionLedger
from .auth import SessionProvider
from .ccapi import CCApiError, CCTradingClient
from .config import TraderConfig
from .executor import (DryRunExecutor, Executor, LiveExecutor,
                       record_markdown, record_sold_holding)
from .holdings import (SECONDS_PER_DAY, best_active_offer, is_due_for_markdown,
                       is_due_for_offer_accept, is_due_for_recheck,
                       markdown_change_is_meaningful, markdown_jitter_factor,
                       markdown_price, next_bump_price, offer_meets_min_market,
                       recheck_decision, should_bump, should_cancel_offer)
from .orders import Order, OrderKind, OrderStatus, plan_to_orders
from .reconcile import StatusSyncer
from .risk import RiskEngine
from .siws import make_session_provider
from .store import HOLDING_HELD, HOLDING_LISTED, Holding, OrderStore
from .strategy import (BuyPlan, build_plan, diagnose_listings,
                       dynamic_bidding_enabled, dynamic_offer_bid,
                       make_candidates, make_offer_candidates)
from .wallet import Wallet

logger = logging.getLogger("collectorcrypt.trader.engine")


def _oracle_price(card: dict[str, Any] | None) -> float | None:
    """Read a held card's current market value from its owned-card payload.

    The owned-cards endpoint carries ``oraclePrice`` (a string such as
    ``"60.92"``) per card. Returns a positive float, or ``None`` when the card
    is missing or the value is absent/unparseable/non-positive (so the caller
    skips the re-check rather than acting on a bad number).
    """
    if not card:
        return None
    raw = card.get("oraclePrice")
    if raw is None:
        return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


@dataclass
class _ActivityState:
    """Net state of *our* wallet after replaying the activity feed.

    Keyed by NFT address. ``buys``/``listings``/``open_offers`` values carry
    ``price``, ``at`` (epoch seconds) and card identity (``name``,
    ``category``, ``card_id``) when the event exposed them; ``exits`` is the
    set of cards that left the wallet within the feed window.
    """

    buys: dict[str, dict[str, Any]] = field(default_factory=dict)
    exits: set[str] = field(default_factory=set)
    listings: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_offers: dict[str, dict[str, Any]] = field(default_factory=dict)


def _event_epoch(event: dict[str, Any]) -> float | None:
    """Parse an activity event's ``createdAt`` ISO timestamp, if present."""
    raw = event.get("createdAt")
    if not raw:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _replay_wallet_activity(feed: list[dict[str, Any]],
                            wallet: str) -> _ActivityState:
    """Reduce the wallet activity feed to our wallet's net state.

    The feed arrives newest-first; replaying it oldest-first lets later
    events naturally supersede earlier ones (an updated offer overrides the
    original, a cancel closes it, a sale closes a listing). Events not
    involving ``wallet`` are skipped entirely. Direction semantics are
    documented on :meth:`TradeEngine._run_activity_sync` and were verified
    against a captured feed.
    """
    state = _ActivityState()
    for event in reversed(feed):
        action = str(event.get("action") or "")
        nft = str(event.get("nftAddress") or "").strip()
        if not nft:
            continue
        from_wallet = str((event.get("from") or {}).get("wallet") or "")
        to_wallet = str((event.get("to") or {}).get("wallet") or "")
        ours_from = from_wallet == wallet
        ours_to = to_wallet == wallet
        if not (ours_from or ours_to):
            continue
        card = event.get("card") or {}
        info: dict[str, Any] = {
            "price": _to_price(event.get("amount")),
            "at": _event_epoch(event),
            "name": str(card.get("itemName") or ""),
            "category": str(card.get("category") or ""),
            "card_id": str(card.get("id") or ""),
        }
        if action == "Sale":
            if ours_to:  # we bought at the listed price
                state.buys[nft] = info
                state.exits.discard(nft)
            elif ours_from:  # our card sold via a direct buy
                state.exits.add(nft)
                state.listings.pop(nft, None)
        elif action == "Offer Accepted":
            if ours_to:  # our standing offer filled -> we bought
                state.buys[nft] = info
                state.exits.discard(nft)
                state.open_offers.pop(nft, None)
            elif ours_from:  # we accepted an incoming offer -> we sold
                state.exits.add(nft)
                state.listings.pop(nft, None)
        elif action in ("Offer Made", "Offer Updated") and ours_from:
            state.open_offers[nft] = info
        elif action == "Offer Cancelled" and ours_from:
            state.open_offers.pop(nft, None)
        elif action in ("List", "Listing Updated") and ours_from:
            state.listings[nft] = info
        elif action == "Unlisted" and ours_from:
            state.listings.pop(nft, None)
    return state


def _to_price(value: Any) -> float | None:
    """Coerce an activity ``amount`` to a positive float, else ``None``."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


class TradeEngine:
    """Runs trade cycles for a single wallet/config."""

    def __init__(self, cfg: TraderConfig, *, client: CCClient | None = None,
                 wallet: Wallet | None = None,
                 store: OrderStore | None = None,
                 session_provider: SessionProvider | None = None,
                 ledger: TransactionLedger | None = None) -> None:
        self._cfg = cfg
        self._client = client or CCClient()
        self._wallet = wallet or Wallet(
            cfg.rpc_url, address=cfg.wallet_address, secret=cfg.wallet_secret
        )
        self._store = store
        # Append-only transaction ledger (provable trade history). Built from
        # the configured path by default; an empty path disables it. Only real
        # money events (the live executor + the sold-signal) ever write to it.
        self._ledger = ledger or TransactionLedger(cfg.ledger_path)
        # The auth seam (ETAPPE 3/4). Defaults to the configured provider, which
        # is the safe NullSessionProvider unless the operator opted into a real
        # one via TRADER_AUTH_PROVIDER. Live execution (ETAPPE 5) uses this.
        self._session_provider = session_provider or make_session_provider(
            cfg, self._wallet
        )

    @property
    def executor(self) -> Executor:
        # Live execution requires ALL of: the master switch, a signing wallet,
        # and a configured (non-null) auth provider. Anything less falls back to
        # the dry-run executor so a half-configured live setup can never spend.
        # The budget-aware variant is built per cycle in run_cycle; this
        # property exposes the gating decision (and the live executor type) with
        # an empty budget for callers that only inspect the mode.
        return self._build_executor(0.0)

    def _is_live_armed(self) -> bool:
        """True only when every live precondition is satisfied."""
        return bool(
            self._cfg.live and self._wallet.can_sign
            and (self._cfg.auth_provider or "none").lower() != "none"
        )

    def _build_executor(self, available_volume: float, *,
                        demo: bool = False) -> Executor:
        """Construct the executor for a cycle, wiring live context when armed.

        The live executor is given everything it needs to run safely: the
        authenticated trading client, the durable store (for incremental state
        + duplicate prevention) and the cycle budget envelope. The dry-run
        executor needs none of this.

        Demo cycles are **always** dry-run: a hypothetical volume must never
        touch the live trading path, even on a fully armed wallet.
        """
        if demo:
            return DryRunExecutor()
        if self._is_live_armed():
            return LiveExecutor(
                self._wallet, self._cfg.rpc_url,
                session_provider=self._session_provider,
                client=CCTradingClient(
                    session_provider=self._session_provider),
                store=self._store,
                available_volume=available_volume,
                cfg=self._cfg,
                ledger=self._ledger,
            )
        return DryRunExecutor()

    # ------------------------------------------------------------------ #
    # Sourcing
    # ------------------------------------------------------------------ #
    def _blacklisted_nfts(self) -> set[str]:
        """NFT addresses flagged unpopular — never re-acquire (Feature 4).

        Best-effort: an unreadable store (or none) yields an empty set, so the
        cycle simply does not filter rather than aborting. The blacklist is an
        acquisition optimization, not a money-safety guard, so failing open here
        cannot move money — the spend/risk gates still apply.
        """
        if self._store is None:
            return set()
        try:
            return set(self._store.blacklisted_nfts())
        except Exception:  # noqa: BLE001 - sourcing must never crash a cycle
            return set()

    def _active_offer_nfts(self) -> set[str]:
        """NFT addresses we already have a resting OPEN offer on.

        These must be excluded from sourcing: re-planning an offer on a card we
        already bid on makes CC reject the duplicate ("New offer price must be
        different from the existing offer price"), which fails the order and can
        trip the consecutive-failure kill switch — stalling all trading. The
        offer bump/cancel maintenance pass owns the lifecycle of an open offer,
        so the acquisition stage must not re-touch it.

        Best-effort: an unreadable store (or none) yields an empty set so the
        cycle simply does not filter rather than aborting.
        """
        if self._store is None:
            return set()
        try:
            return {o.nft for o in self._store.open_offers() if o.nft}
        except Exception:  # noqa: BLE001 - sourcing must never crash a cycle
            return set()

    def _collect_listings(self) -> list[dict[str, Any]]:
        """Fetch + normalize listings across the configured categories/pages.

        The marketplace API ignores the category param (every category returns
        the same pages), so each page is fetched **once** and partitioned
        client-side against the configured categories (mirrors ScanManager).
        An empty category set means all categories qualify.
        """
        from .. import config as app_config

        wanted = {c.strip().lower() for c in self._cfg.categories if c.strip()}

        seen: set[str] = set()
        cards: list[dict[str, Any]] = []
        for page in range(1, self._cfg.max_pages + 1):
            data = self._client.fetch_marketplace_page_with_retry(
                page, app_config.SCAN_STEP
            )
            raw = data.get("filterNFtCard") or []
            if not raw:
                break
            for c in raw:
                n = normalize_card(c)
                nft = n.get("nft")
                if not nft or nft in seen:
                    continue
                if wanted and (n.get("category") or "").lower() not in wanted:
                    continue
                seen.add(nft)
                cards.append(n)
            if page >= int(data.get("totalPages") or 1):
                break
        return cards

    # ------------------------------------------------------------------ #
    # Cycle
    # ------------------------------------------------------------------ #
    def run_cycle(self, *, sim_volume: float | None = None,
                  persist: bool = True) -> dict[str, Any]:
        """Run one decision cycle.

        When ``sim_volume`` is given the wallet is **not** read at all; the bot
        plans against that hypothetical USDC volume instead. This powers the
        "demo mode" in the UI, letting the user see how the bot would react to
        any budget without owning the funds (and without a configured wallet).

        When a store is attached and ``persist`` is true, the cycle header and
        all resulting orders are written durably. Demo cycles are never
        persisted (they did not happen on-chain and must not pollute the real
        order ledger).
        """
        sol_rate = self._client.fetch_sol_usd()
        demo = sim_volume is not None
        if demo:
            sol_balance = 0.0
            usdc_balance = max(0.0, float(sim_volume))
            available_volume = max(0.0, float(sim_volume))
        else:
            sol_balance = self._wallet.sol_balance()
            usdc_balance = self._wallet.usdc_balance()
            available_volume = max(
                0.0, usdc_balance - max(0.0, self._cfg.reserve_usdc)
            )

        # Feature 2 (min-operate gate): on real cycles, if the available volume
        # is below the operator's configured minimum, pause *acquisition* for
        # this cycle — source nothing and build no new buys/offers — while still
        # running inventory maintenance (status sync, exit/relist) below. We keep
        # managing what we already own and only stop acquiring. This is the
        # earliest point the real balance is known, so we avoid even fetching
        # listings we cannot act on. Demo cycles are exempt (hypothetical volume).
        min_operate = max(0.0, float(self._cfg.min_operate_usd))
        acquisition_paused = (not demo and min_operate > 0
                              and available_volume < min_operate)
        pause_reason = ""
        if acquisition_paused:
            pause_reason = (f"paused: volume ${available_volume:.2f} "
                            f"< min ${min_operate:.2f}")
            listings: list[dict[str, Any]] = []
            candidates: list = []
            offer_candidates: list = []
            plan = build_plan([], available_volume, self._cfg,
                              offer_candidates=[])
            near_misses: list = []
        else:
            # Feature 4: drop unpopular (blacklisted) NFTs from sourcing so the
            # bot never re-acquires a card it already struggled to sell. Cards
            # we already have a resting OPEN offer on are excluded too, so the
            # bot does not re-bid an existing offer (CC rejects that duplicate,
            # which would fail the order and can trip the failure kill switch);
            # those offers are managed by the bump/cancel maintenance pass.
            blacklist = self._blacklisted_nfts() | self._active_offer_nfts()
            listings = self._collect_listings()
            candidates = make_candidates(listings, sol_rate, self._cfg,
                                         blacklist)
            offer_candidates = make_offer_candidates(listings, sol_rate,
                                                     self._cfg, blacklist)
            plan = build_plan(candidates, available_volume, self._cfg,
                              offer_candidates=offer_candidates)
            # Always surface the closest deals (qualifying or not) so the UI /
            # demo can show which hypothetical cards are near the buy threshold.
            near_misses = diagnose_listings(listings, sol_rate, self._cfg,
                                            plan.card_cap_usd)

        executor = self._build_executor(available_volume, demo=demo)
        live = isinstance(executor, LiveExecutor) and not demo
        cycle_id = uuid.uuid4().hex
        # Feature (escrow-leak fix): before the offers are frozen into orders,
        # reprice them against the live order book on real cycles. Blind offers
        # lock escrow on bids that can never win; dynamic range bidding instead
        # bids our opening lowball when a card is uncontested, just outbids a
        # competitor inside our range, or skips the card when winning would cost
        # more than our ceiling/budget. Dry-run/demo cannot read the live order
        # book, so they quote the opening lowball as an explicit uncontested
        # assumption (marked in the report) instead of silently falling back to
        # the static bid — the simulation then reflects the configured range.
        # Acquisition-paused cycles have no offers to reprice.
        if not acquisition_paused:
            report_offer_pricing = self._reprice_offers_dynamically(
                plan, read_book=live)
        else:
            report_offer_pricing = None
        # The plan is materialised into typed PLANNED orders; the executor then
        # transitions them (dry-run resolves in-memory, live would send/sign/
        # broadcast). Both modes share this one pipeline.
        planned_orders = plan_to_orders(plan, cycle_id, simulated=not live)

        # Risk gate (ETAPPE 7): the final guard before any live order is sent.
        # On live cycles, operator-set limits and the kill switch decide which
        # planned orders may proceed; blocked orders are failed safely (never
        # sent) and never reach the executor. Dry-run/demo are unaffected but
        # the posture is still computed for display. ``require_caps`` makes the
        # engine refuse a live cycle outright when no limit is configured (R2),
        # so the bot can never run uncapped with real funds.
        risk_decision = None
        risk_blocked: list[Order] = []
        if live:
            risk_decision = RiskEngine(self._cfg, self._store).evaluate(
                planned_orders, require_caps=True)
            for order, reason in risk_decision.blocked:
                if not order.is_terminal:
                    order.transition(OrderStatus.FAILED,
                                    detail=f"risk gate: {reason}",
                                    error=f"blocked by risk limit: {reason}")
            risk_blocked = risk_decision.blocked_orders
            executable = risk_decision.allowed
        else:
            executable = planned_orders

        executed = executor.execute(executable) if executable else []
        # The report sees every planned order: those executed plus those the
        # risk gate failed (so blocked intents are visible, not silently gone).
        orders = executed + risk_blocked

        report = _report(self._cfg, self._wallet, sol_rate, sol_balance,
                         usdc_balance, available_volume, listings, candidates,
                         plan, orders, live, demo=demo, near_misses=near_misses,
                         cycle_id=cycle_id)
        if risk_decision is not None:
            report["risk"] = risk_decision.posture
        if acquisition_paused:
            report["acquisition_paused"] = True
            report["pause_reason"] = pause_reason
        if report_offer_pricing is not None:
            report["offer_pricing"] = report_offer_pricing

        # Live maintenance: after placing new buys/offers, (1) reconcile any
        # in-flight orders against CollectorCrypt's authoritative status, then
        # (2) run the exit flow to list cards from confirmed buys. The order
        # matters — a buy confirmed by the sync spawns a relist candidate that
        # the exit pass can then list in the same cycle. Both are no-ops in
        # dry-run/demo. The exit pass is skipped while the risk kill switch is
        # tripped (something is wrong; do not sign/send more), but the
        # read-only status sync still runs to resolve in-flight state.
        if live and self._store is not None:
            report["activity_sync"] = self._run_activity_sync()
            report["status_sync"] = self._run_status_sync()
            # Authoritative sold-signal: mark holdings sold once they leave the
            # wallet. Runs before the maintenance passes so a freshly-sold card
            # is excluded from bump/markdown/accept this same cycle, and even
            # while halted (it is read-only network + a holdings-only write).
            report["ownership_sync"] = self._run_ownership_sync()
            halted = bool(risk_decision and risk_decision.halted)
            if halted:
                report["relisted"] = []
                report["bumped"] = []
                report["cancelled"] = []
                report["marked_down"] = []
                report["offers_accepted"] = []
            else:
                report["relisted"] = self._run_exit_flow(executor)
                report["bumped"] = self._run_offer_bump_pass(executor)
                report["cancelled"] = self._run_offer_cancel_pass(executor)
                report["marked_down"] = self._run_markdown_pass(executor)
                report["offers_accepted"] = self._run_accept_offer_pass(executor)
            # The market re-check is a read-only inventory pass, so it runs even
            # while the kill switch is tripped (it never signs or spends).
            report["market_recheck"] = self._run_market_recheck()


        # Durable persistence: real cycles only. Demo never touches the ledger.
        if self._store is not None and persist and not demo:
            self._persist_cycle(cycle_id, report, orders)
        return report

    # ------------------------------------------------------------------ #
    # Live maintenance (ETAPPE 6)
    # ------------------------------------------------------------------ #
    def _run_activity_sync(self) -> dict[str, Any]:
        """Rebuild durable state from the wallet's on-chain activity feed.

        Runs first in every live cycle so a restarted bot — same or new
        settings, even a fresh database — immediately knows which cards it
        owns, which of those are listed, what each one cost, and which of its
        offers are still resting on the book. Only events *initiated by or
        settling to* our wallet matter; everything else in the feed is other
        people's traffic and is ignored.

        Replay semantics (verified against the captured feed):

        * ``Sale`` — ``from`` is the seller, ``to`` the buyer. ``to == us``
          means we bought (``amount`` is our cost basis); ``from == us`` means
          our card sold.
        * ``Offer Accepted`` — same asset direction: ``from`` is the accepting
          seller, ``to`` the bidder whose offer filled. ``to == us`` means our
          standing offer filled (we bought at ``amount``); ``from == us``
          means we accepted an incoming offer (we sold; ``amount`` is the
          *net* proceeds after the marketplace fee).
        * ``Offer Made`` / ``Offer Updated`` from us — our offer is resting at
          ``amount``; ``Offer Cancelled`` from us closes it.
        * ``List`` / ``Listing Updated`` from us — the card is listed at
          ``amount``; ``Unlisted`` from us (or any sale) clears that.

        Store writes are strictly additive/backfilling: existing holdings only
        gain a cost basis (when ``cost_usd`` is 0) or listing state (when
        unlisted in the store but listed on-chain); cards we bought per the
        feed but that are missing from the store entirely are recreated; open
        offers absent from the store are re-injected as ``OPEN`` orders.
        Nothing is ever overwritten or marked sold here — the ownership sync
        that runs right after this pass stays the authoritative exit signal.

        Fails safe: any fetch error recovers nothing and surfaces ``error``.
        """
        if self._store is None:
            return {"checked": 0, "recovered_offers": [],
                    "recovered_holdings": [], "backfilled": []}
        try:
            client = CCTradingClient(session_provider=self._session_provider)
            payload = client.get_wallet_activity(wallet=self._wallet.address)
        except CCApiError as exc:
            return {"error": f"activity fetch failed: {exc}"}
        except Exception as exc:  # noqa: BLE001 - maintenance must never crash a cycle
            return {"error": f"activity sync error: {exc}"}
        feed = payload.get("data") or []
        state = _replay_wallet_activity(feed, self._wallet.address)
        report: dict[str, Any] = {"checked": len(feed),
                                  "recovered_offers": [],
                                  "recovered_holdings": [],
                                  "backfilled": []}
        try:
            self._apply_activity_state(state, report)
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"activity apply error: {exc}"
        return report

    def _apply_activity_state(self, state: "_ActivityState",
                              report: dict[str, Any]) -> None:
        """Write the replayed activity state into the store (additive only)."""
        store = self._store
        assert store is not None  # guarded by caller
        now = time.time()

        # --- Holdings: recreate missing buys, backfill cost/listing state ---
        for nft, buy in state.buys.items():
            if nft in state.exits:
                continue  # bought and later sold inside the feed window
            holding = store.get_holding(nft)
            listing = state.listings.get(nft)
            if holding is None:
                holding = Holding(
                    nft=nft,
                    name=buy.get("name", ""),
                    category=buy.get("category", ""),
                    acquired_at=buy.get("at") or now,
                    cost_usd=buy.get("price") or 0.0,
                    market_usd_at_buy=buy.get("price") or 0.0,
                )
                if listing is not None:
                    holding.status = HOLDING_LISTED
                    holding.listed_at = listing.get("at") or now
                    holding.list_price_usd = listing.get("price")
                store.upsert_holding(holding)
                report["recovered_holdings"].append(
                    {"nft": nft, "cost_usd": holding.cost_usd,
                     "listed": listing is not None})
                continue
            changed: list[str] = []
            if holding.cost_usd <= 0.0 and (buy.get("price") or 0.0) > 0.0:
                # The cost basis is immutable in the store; this dedicated
                # path fills it ONLY while it is still unknown (== 0).
                if store.backfill_cost_basis(nft, float(buy["price"])):
                    holding.cost_usd = float(buy["price"])
                    changed.append("cost_usd")
                if holding.market_usd_at_buy <= 0.0:
                    holding.market_usd_at_buy = float(buy["price"])
                    changed.append("market_usd_at_buy")
            if (listing is not None and holding.listed_at is None
                    and holding.status == HOLDING_HELD):
                holding.status = HOLDING_LISTED
                holding.listed_at = listing.get("at") or now
                holding.list_price_usd = listing.get("price")
                changed.append("listing")
            if changed:
                store.upsert_holding(holding)
                report["backfilled"].append({"nft": nft, "fields": changed})

        # --- Offers: re-inject open offers the store does not know about ---
        known = {o.nft for o in store.open_offers()}
        for nft, offer in state.open_offers.items():
            if nft in known:
                continue
            order = Order(
                kind=OrderKind.OFFER,
                nft=nft,
                name=offer.get("name", ""),
                category=offer.get("category", ""),
                status=OrderStatus.OPEN,
                price_usd=offer.get("price") or 0.0,
                simulated=False,
                card_id=offer.get("card_id", ""),
                client_order_id=f"recovered:{int(now * 1000)}:{nft}",
                detail="recovered from wallet activity feed",
            )
            store.upsert_order(order)
            report["recovered_offers"].append(
                {"nft": nft, "price_usd": order.price_usd})

    def _run_status_sync(self) -> dict[str, Any]:
        """Reconcile in-flight orders against CC's authoritative status.

        Never raises: a maintenance failure must not abort a trading cycle. On
        error the report carries an ``error`` field for the operator.
        """
        try:
            syncer = StatusSyncer(
                self._store,  # type: ignore[arg-type]
                client=CCTradingClient(session_provider=self._session_provider),
                wallet=self._wallet.address,
            )
            return syncer.sync().to_dict()
        except CCApiError as exc:
            return {"error": f"status sync failed: {exc}"}
        except Exception as exc:  # noqa: BLE001 - maintenance must never crash a cycle
            return {"error": f"status sync error: {exc}"}

    def _run_ownership_sync(self) -> dict[str, Any]:
        """Mark holdings ``sold`` once they leave the wallet (the sold-signal).

        The owned-cards endpoint lists only currently-owned NFTs, so a held
        card that is **absent** from it has sold (or been transferred away) —
        there is no per-card "Sold" status to read. This is the authoritative
        exit signal that stops further bump/markdown/blacklist work on a card.

        Fails safe: it only marks a card sold when the *complete* owned set was
        fetched confidently (all pages). Any fetch error marks nothing sold and
        surfaces an ``error`` for the operator. Never signs or spends.
        """
        if self._store is None:
            return {"checked": 0, "sold": []}
        try:
            held = self._store.held_cards()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not load holdings: {exc}"}
        if not held:
            return {"checked": 0, "sold": []}
        try:
            owned = self._fetch_owned_nfts()
        except CCApiError as exc:
            return {"error": f"owned-cards fetch failed: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"owned-cards error: {exc}"}
        now = time.time()
        sold: list[dict[str, Any]] = []
        for holding in held:
            if not holding.nft or holding.nft in owned:
                continue
            try:
                record_sold_holding(self._store, holding, now=now)
            except Exception as exc:  # noqa: BLE001
                sold.append({"nft": holding.nft, "name": holding.name,
                             "error": str(exc)})
                continue
            self._record_sold_txn(holding, now=now)
            sold.append({"nft": holding.nft, "name": holding.name})
        return {"checked": len(held), "sold": sold}

    def _fetch_owned_nfts(self) -> set[str]:
        """Build the complete set of NFT addresses the wallet currently owns.

        Pages through every result page so a card on a later page is never
        mistaken for sold. A hard page cap bounds the loop. Any page error
        propagates so the caller fails safe (marks nothing sold).
        """
        return set(self._fetch_owned_cards())

    def _record_sold_txn(self, holding: Holding, *, now: float) -> None:
        """Append a ``sold`` row to the transaction ledger for an exited card.

        A card detected as sold via ownership reconcile left the wallet without
        a broadcast of ours, so there is no signature; the listed price is the
        best available proceeds estimate. Best-effort: never aborts the cycle.
        """
        try:
            self._ledger.record(
                event="sold",
                kind="list",
                card_name=holding.name or "",
                category=holding.category or "",
                nft_address=holding.nft or "",
                price_usd=holding.list_price_usd or 0.0,
                market_usd=(holding.market_usd_current
                            or holding.market_usd_at_buy or 0.0),
                status="sold",
                detail="card left wallet (sold/transferred)",
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - a ledger write must never abort a cycle
            logger.error("Failed to record sold transaction for %s: %s",
                         holding.nft, exc)

    def _fetch_owned_cards(self) -> dict[str, dict[str, Any]]:
        """Map every currently-owned NFT address to its raw owned-card payload.

        Pages through all result pages (so a card on a later page is never
        lost). Each card carries ``oraclePrice`` (the per-card market value) and
        ``listing``/``listedAt`` (held vs listed). Any page error propagates so
        callers fail safe.
        """
        client = CCTradingClient(session_provider=self._session_provider)
        wallet = self._wallet.address
        owned: dict[str, dict[str, Any]] = {}
        page = 1
        max_pages = 50
        while page <= max_pages:
            payload = client.get_owned_cards(wallet=wallet, page=page, step=96)
            cards = payload.get("filterNFtCard") or []
            for card in cards:
                nft = str(card.get("nftAddress") or "").strip()
                if nft:
                    owned[nft] = card
            try:
                total_pages = int(payload.get("totalPages") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                break
            page += 1
        return owned

    def _run_exit_flow(self, executor: Executor) -> list[dict[str, Any]]:
        """List cards from confirmed buys (the live exit/relisting flow).

        Loads the persisted ``PLANNED`` relist candidates and drives each onto
        the market via the live executor. Returns a compact per-listing summary
        for the report/UI. Never raises.
        """
        if not isinstance(executor, LiveExecutor):
            return []
        try:
            candidates = self._store.relist_candidates()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"could not load relist candidates: {exc}"}]
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            listed = executor.relist(candidate)
            results.append({
                "name": listed.name,
                "nft": listed.nft,
                "status": listed.status.value,
                "price_usd": round(listed.price_usd, 2),
                "market_usd": round(listed.market_usd, 2),
                "ok": listed.succeeded,
                "detail": listed.detail,
                "category": listed.category,
            })
        return results

    # ------------------------------------------------------------------ #
    # Inventory maintenance passes (ETAPPE 6)
    # ------------------------------------------------------------------ #
    # Each pass selects its due set with the Etappe 1 store queries, decides
    # with the Etappe 3 pure functions, and drives the action through the live
    # executor. The executor's maintenance actions are SAFE-FAILURE until
    # Etappe 8 verifies the request shapes, so these passes are observable but
    # move no money yet. All run only on the armed live executor and never raise.
    def _reprice_offers_dynamically(
            self, plan: BuyPlan, *, read_book: bool = True
    ) -> list[dict[str, Any]] | None:
        """Reprice planned offers against the live order book (escrow-leak fix).

        Blind offering locks escrow on bids that can never win. When dynamic
        range bidding is configured (:func:`dynamic_bidding_enabled`), each
        planned offer is re-quoted from the card's live activity feed instead:
        bid our opening lowball when uncontested, just outbid the best competing
        bid while it stays inside our range, or drop the offer when winning would
        breach our ceiling, the per-card cap, the remaining budget or the resale
        profit floor — keeping escrow for bids that can actually fill.

        Offers are processed cheapest-first against a running budget so the
        limited escrow funds the most affordable wins first. The plan's
        ``offers`` list is replaced in place with the kept, repriced offers.
        Returns a per-offer report, or ``None`` when dynamic bidding is off (the
        static planned bids stand and nothing is recorded). Fails safe: if a
        card's feed cannot be read, the static planned bid is kept when it still
        fits the budget, otherwise the offer is dropped.

        When ``read_book`` is false (dry-run/demo) there is no live order book to
        read, so every card is treated as uncontested and quoted at the opening
        lowball; each such entry is marked ``assumed`` so the simulation makes
        the missing competition explicit.
        """
        if not dynamic_bidding_enabled(self._cfg):
            return None
        cfg = self._cfg
        our_wallet = self._wallet.address
        ordered = sorted(plan.offers, key=lambda o: o.candidate.ask_usd)
        remaining = max(0.0, float(plan.offer_budget))
        kept: list = []
        report: list[dict[str, Any]] = []
        for offer in ordered:
            cand = offer.candidate
            cap = min(float(plan.card_cap_usd), remaining, float(cand.resell_usd))
            if read_book:
                try:
                    feed = self._fetch_card_activity(cand.nft)
                    top = best_active_offer(feed, exclude_wallet=our_wallet)
                    highest_other = top.amount if top is not None else None
                    price = dynamic_offer_bid(cand.ask_usd, highest_other, cfg,
                                              max_price=cap)
                except Exception as exc:  # noqa: BLE001 - must not crash
                    # Fail safe: fall back to the static planned bid if it fits.
                    fallback = round(float(offer.offer_usd), 2)
                    if (fallback > 0 and fallback <= cap
                            and fallback < cand.resell_usd):
                        offer.offer_usd = fallback
                        remaining -= fallback
                        kept.append(offer)
                        report.append({
                            "nft": cand.nft, "name": cand.name,
                            "offer_usd": fallback, "status": "fallback",
                            "detail": f"order book unreadable: {exc}",
                        })
                    else:
                        report.append({
                            "nft": cand.nft, "name": cand.name,
                            "status": "skipped",
                            "detail": f"order book unreadable: {exc}",
                        })
                    continue
                status = "repriced"
                detail: str | None = None
            else:
                # Simulation: no live order book -> assume the card is
                # uncontested and quote the opening lowball.
                price = dynamic_offer_bid(cand.ask_usd, None, cfg, max_price=cap)
                status = "assumed"
                detail = ("assumed uncontested (no live order book in "
                          "simulation)")
            if price is None:
                report.append({
                    "nft": cand.nft, "name": cand.name, "status": "skipped",
                    "detail": "no winnable price within range/budget",
                })
                continue
            price = round(float(price), 2)
            # Strict guards: never breach the cap or give up the resale profit.
            if price <= 0 or price > cap or price >= cand.resell_usd:
                report.append({
                    "nft": cand.nft, "name": cand.name, "status": "skipped",
                    "detail": "price exceeds cap or resale floor",
                })
                continue
            offer.offer_usd = price
            remaining -= price
            kept.append(offer)
            entry: dict[str, Any] = {
                "nft": cand.nft, "name": cand.name,
                "offer_usd": price, "status": status,
            }
            if detail:
                entry["detail"] = detail
            report.append(entry)
        plan.offers = kept
        return report

    def _run_offer_bump_pass(self, executor: Executor) -> list[dict[str, Any]]:
        """Bump aged open offers to re-trigger the owner's notification."""
        if not isinstance(executor, LiveExecutor):
            return []
        try:
            offers = self._store.open_offers()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - maintenance must not crash a cycle
            return [{"error": f"could not load open offers: {exc}"}]
        now = time.time()
        results: list[dict[str, Any]] = []
        for order in offers:
            if not should_bump(order, self._cfg, now):
                continue
            # Self-bidding guard: a bump exists to re-surface our offer in the
            # owner's notifications, not to raise it. If the live order book
            # already shows us as the highest bidder, bumping would lift our own
            # escrow against ourselves for nothing, so skip it this cycle. The
            # read fails open — if the feed cannot be read we bump as before
            # (the safe, pre-existing behaviour) rather than stalling the nudge.
            try:
                feed = self._fetch_card_activity(order.nft)
                top = best_active_offer(feed)
            except Exception:  # noqa: BLE001 - maintenance must not crash a cycle
                top = None
            if top is not None and top.buyer == self._wallet.address:
                results.append({
                    "nft": order.nft,
                    "name": order.name,
                    "status": "skipped",
                    "detail": "already highest bidder",
                })
                continue
            new_price = next_bump_price(order, self._cfg)
            out = executor.bump_offer(order, new_price)
            results.append({
                "nft": out.nft,
                "name": out.name,
                "new_price_usd": round(new_price, 2),
                "bump_count": out.bump_count,
                "status": out.status.value,
                "detail": out.detail,
            })
        return results

    def _run_offer_cancel_pass(self, executor: Executor) -> list[dict[str, Any]]:
        """Cancel offers that exhausted their bumps and still did not fill."""
        if not isinstance(executor, LiveExecutor):
            return []
        try:
            offers = self._store.open_offers()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"could not load open offers: {exc}"}]
        now = time.time()
        results: list[dict[str, Any]] = []
        for order in offers:
            if not should_cancel_offer(order, self._cfg, now):
                continue
            out = executor.cancel_offer(order)
            results.append({
                "nft": out.nft,
                "name": out.name,
                "status": out.status.value,
                "detail": out.detail,
            })
        return results

    def _run_markdown_pass(self, executor: Executor) -> list[dict[str, Any]]:
        """Step down the price of listed-but-unsold cards toward the cost floor."""
        if not isinstance(executor, LiveExecutor):
            return []
        jitter_pct = max(0.0, float(self._cfg.markdown_jitter_pct))
        # The jitter can pull a step *earlier* than the nominal delay, so widen
        # the coarse store query by the maximum negative jitter; the pure
        # ``is_due_for_markdown`` then re-checks each card with its own exact,
        # deterministic jittered delay so nothing fires too soon.
        jitter_floor = max(0.0, 1.0 - jitter_pct / 100.0)
        delay_sec = (max(0.0, self._cfg.markdown_delay_days) * jitter_floor
                     * SECONDS_PER_DAY)
        # Coarse step ceiling for the query; the pure logic enforces the floor.
        step_pct = max(0.01, float(self._cfg.markdown_step_pct))
        max_steps = max(1, int(100.0 / step_pct) + 1)
        try:
            due = self._store.holdings_due_for_markdown(  # type: ignore[union-attr]
                min_listed_age_sec=delay_sec, max_steps=max_steps)
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"could not load markdown candidates: {exc}"}]
        now = time.time()
        results: list[dict[str, Any]] = []
        for holding in due:
            # Deterministic per-card, per-step jitter (anti-snipe): the same
            # holding+step always resolves to the same timing and step size, so
            # the markdown curve is stable per card yet unpredictable across the
            # inventory and cannot be reverse-engineered and waited out.
            interval_jitter = markdown_jitter_factor(
                f"{holding.nft}:int:{holding.markdown_steps}", jitter_pct)
            step_jitter = markdown_jitter_factor(
                f"{holding.nft}:step:{holding.markdown_steps}", jitter_pct)
            if not is_due_for_markdown(holding, self._cfg, now,
                                       interval_jitter=interval_jitter):
                continue
            new_price = markdown_price(holding, self._cfg,
                                       step_jitter=step_jitter)
            old_price = round(holding.list_price_usd or 0.0, 2)
            # Gas guard: never send a markdown whose drop is too small to be
            # worth the on-chain fee. The pure check decides; we record the skip
            # so the tiny step is visible but no transaction is signed.
            if not markdown_change_is_meaningful(holding.list_price_usd or 0.0,
                                                 new_price, self._cfg):
                results.append({
                    "nft": holding.nft,
                    "name": holding.name,
                    "old_price_usd": old_price,
                    "new_price_usd": round(new_price, 2),
                    "status": "skipped",
                    "detail": "price change below minimum",
                })
                continue
            order = self._listing_order_for(holding)
            out = executor.markdown_listing(order, new_price)
            if out.status is OrderStatus.CONFIRMED:
                # The live markdown settled -> advance the curve on the holding.
                try:
                    record_markdown(self._store, holding, new_price, now=now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("markdown persist failed for %s: %s",
                                   holding.nft, exc)
            results.append({
                "nft": holding.nft,
                "name": holding.name,
                "old_price_usd": old_price,
                "new_price_usd": round(new_price, 2),
                "status": out.status.value,
                "detail": out.detail,
            })
        return results

    def _run_accept_offer_pass(self, executor: Executor) -> list[dict[str, Any]]:
        """Accept the best incoming bid on cards parked at the cost floor.

        For each floored holding old enough to accept, the live card-activity
        feed is read and the best still-open incoming offer is reconstructed
        (``best_active_offer``). The bid must clear the configurable min-% of
        market value (``offer_meets_min_market``) before the live accept fires;
        on a confirmed settle the holding is marked sold. Reading the feed and
        the accept are the live steps, now driven by the verified
        ``card-activity`` / ``accept-offer`` shapes (Etappe 8.3).
        """
        if not isinstance(executor, LiveExecutor):
            return []
        delay_sec = max(0.0, self._cfg.markdown_delay_days) * SECONDS_PER_DAY
        try:
            due = self._store.holdings_due_for_offer_accept(  # type: ignore[union-attr]
                min_listed_age_sec=delay_sec)
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"could not load offer-accept candidates: {exc}"}]
        now = time.time()
        results: list[dict[str, Any]] = []
        for holding in due:
            if not is_due_for_offer_accept(holding, self._cfg, now):
                continue
            try:
                feed = self._fetch_card_activity(holding.nft)
            except CCApiError as exc:
                results.append({"nft": holding.nft, "name": holding.name,
                                "error": f"offer read failed: {exc}"})
                continue
            except Exception as exc:  # noqa: BLE001
                results.append({"nft": holding.nft, "name": holding.name,
                                "error": f"offer read error: {exc}"})
                continue
            offer = best_active_offer(feed)
            if offer is None:
                results.append({"nft": holding.nft, "name": holding.name,
                                "status": "skipped",
                                "detail": "no active incoming offer"})
                continue
            if not offer_meets_min_market(offer.amount, holding, self._cfg):
                results.append({
                    "nft": holding.nft, "name": holding.name,
                    "offer_usd": round(offer.amount, 2), "buyer": offer.buyer,
                    "status": "skipped",
                    "detail": "best offer below min market %"})
                continue
            order = self._listing_order_for(holding)
            out = executor.accept_offer(order, offer.buyer, offer.amount)
            if out.status is OrderStatus.CONFIRMED:
                try:
                    record_sold_holding(self._store, holding, now=now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("sold persist failed for %s: %s",
                                   holding.nft, exc)
            results.append({
                "nft": holding.nft,
                "name": holding.name,
                "offer_usd": round(offer.amount, 2),
                "buyer": offer.buyer,
                "status": out.status.value,
                "detail": out.detail,
            })
        return results

    def _fetch_card_activity(self, nft: str) -> list[dict[str, Any]]:
        """Read a card's on-chain activity feed (newest first).

        The verified ``card-activity`` endpoint returns a bare JSON array which
        the transport wraps as ``{"data": [...]}``; this unwraps it to the raw
        list for :func:`best_active_offer`. A read, so it is retryable.
        """
        client = CCTradingClient(session_provider=self._session_provider)
        payload = client.get_card_activity(nft=nft)
        feed = payload.get("data") if isinstance(payload, dict) else payload
        return feed if isinstance(feed, list) else []

    def _run_market_recheck(self) -> dict[str, Any]:
        """Re-check held cards' current market value (feature 5b).

        The market value of a single owned NFT is read from ``oraclePrice`` in
        the verified owned-cards endpoint (DevTools 2026-06-07). For each held,
        unsold card due for a re-check, ``recheck_decision`` decides: on a
        **positive** change it raises the resale target and restarts the sell
        cycle at day 0 (resets the markdown clock); on a flat/negative change
        nothing resets and the markdown curve continues. Either way the last
        re-checked value and timestamp are persisted.

        Read-only on the network (it never signs or spends), so it runs even
        while the kill switch is tripped. Fails safe: any fetch error re-checks
        nothing. A card absent from the owned set is left to ``ownership_sync``
        (it has sold) and skipped here.
        """
        if self._store is None:
            return {"checked": 0, "raised": []}
        try:
            held = self._store.held_cards()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not load holdings: {exc}"}
        now = time.time()
        due = [h for h in held if is_due_for_recheck(h, self._cfg, now)]
        if not due:
            return {"checked": 0, "raised": []}
        try:
            owned = self._fetch_owned_cards()
        except CCApiError as exc:
            return {"error": f"owned-cards fetch failed: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"owned-cards error: {exc}"}
        checked = 0
        raised: list[dict[str, Any]] = []
        for holding in due:
            price = _oracle_price(owned.get(holding.nft))
            if price is None:
                continue  # absent (sold -> ownership_sync) or no price -> skip
            checked += 1
            decision = recheck_decision(holding, price, self._cfg)
            holding.market_usd_current = price
            holding.market_checked_at = now
            if decision.raised:
                holding.market_usd_at_buy = decision.new_market_usd_at_buy
                holding.list_price_usd = decision.new_list_price
                # Restart the sell cycle at day 0 (reset the markdown timers).
                holding.markdown_steps = 0
                holding.last_markdown_at = None
                holding.listed_at = now if holding.listed_at is not None else None
                raised.append({
                    "nft": holding.nft,
                    "name": holding.name,
                    "new_market_usd": round(price, 2),
                    "new_list_price_usd": round(decision.new_list_price, 2),
                })
            try:
                self._store.upsert_holding(holding)
            except Exception as exc:  # noqa: BLE001
                logger.warning("market recheck persist failed for %s: %s",
                               holding.nft, exc)
        return {"checked": checked, "raised": raised}

    def _listing_order_for(self, holding: Holding) -> Order:
        """Build a transient ``LIST`` order describing a held card's listing.

        Used as the subject of the markdown/accept maintenance actions. It is a
        real (non-simulated) order so the live executor treats it correctly, but
        it is never persisted by these passes (the executor actions are safe
        no-ops until Etappe 8).
        """
        price = holding.list_price_usd or holding.market_usd_at_buy
        return Order(
            kind=OrderKind.LIST,
            nft=holding.nft,
            name=holding.name,
            category=holding.category,
            price_usd=float(price),
            market_usd=float(holding.market_usd_at_buy),
            resell_usd=float(price),
            simulated=False,
        )

    def _persist_cycle(self, cycle_id: str, report: dict[str, Any],
                       orders: list[Order]) -> None:
        """Write the cycle header (with a redacted config snapshot) and orders."""
        try:
            self._store.save_cycle(  # type: ignore[union-attr]
                cycle_id,
                mode=report["mode"],
                wallet=report["wallet"],
                demo=report["demo"],
                config_snapshot=_config_snapshot(self._cfg),
                summary=_cycle_summary(report),
            )
            self._store.save_orders(orders)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - persistence must not crash a cycle
            report["persist_error"] = f"Failed to persist cycle: {exc}"


def _order_states(orders: list[Order]) -> dict[str, int]:
    """Count orders by status — a compact, observable cycle summary."""
    counts: dict[str, int] = {}
    for o in orders:
        counts[o.status.value] = counts.get(o.status.value, 0) + 1
    return counts


def _config_snapshot(cfg: TraderConfig) -> dict[str, Any]:
    """A redacted snapshot of the config that drove a cycle.

    The wallet **secret is never included** — only its presence is recorded as
    a boolean so an operator can later see whether the cycle ran with signing
    capability, without the key ever touching the database.
    """
    return {
        "rpc_url": cfg.rpc_url,
        "wallet_address": cfg.wallet_address,
        "has_secret": cfg.has_secret,
        "live": cfg.live,
        "reserve_usdc": cfg.reserve_usdc,
        "gas_reserve_sol": cfg.gas_reserve_sol,
        "base_max_card_usd": cfg.base_max_card_usd,
        "min_card_usd": cfg.min_card_usd,
        "min_discount_pct": cfg.min_discount_pct,
        "direct_buy_pct": cfg.direct_buy_pct,
        "offer_pct": cfg.offer_pct,
        "offer_discount_pct": cfg.offer_discount_pct,
        "offer_open_discount_pct": cfg.offer_open_discount_pct,
        "offer_ceiling_pct": cfg.offer_ceiling_pct,
        "offer_increment_usd": cfg.offer_increment_usd,
        "offer_max_premium_pct": cfg.offer_max_premium_pct,
        "resell_discount_pct": cfg.resell_discount_pct,
        "escalation_volume_usd": cfg.escalation_volume_usd,
        "escalation_max_card_usd": cfg.escalation_max_card_usd,
        "max_spend_per_cycle_usd": cfg.max_spend_per_cycle_usd,
        "max_spend_per_day_usd": cfg.max_spend_per_day_usd,
        "max_open_positions": cfg.max_open_positions,
        "max_consecutive_failures": cfg.max_consecutive_failures,
        "offer_bump_usd": cfg.offer_bump_usd,
        "offer_bump_age_hours": cfg.offer_bump_age_hours,
        "offer_bump_max": cfg.offer_bump_max,
        "min_operate_usd": cfg.min_operate_usd,
        "max_owned_cards": cfg.max_owned_cards,
        "unpopular_days": cfg.unpopular_days,
        "markdown_delay_days": cfg.markdown_delay_days,
        "markdown_step_pct": cfg.markdown_step_pct,
        "markdown_interval_days": cfg.markdown_interval_days,
        "markdown_jitter_pct": cfg.markdown_jitter_pct,
        "markdown_min_change_usd": cfg.markdown_min_change_usd,
        "offer_accept_delay_days": cfg.offer_accept_delay_days,
        "offer_accept_min_market_pct": cfg.offer_accept_min_market_pct,
        "market_recheck_hours": cfg.market_recheck_hours,
        "categories": list(cfg.categories),
        "max_pages": cfg.max_pages,
        "allowed_marketplaces": list(cfg.allowed_marketplaces),
    }


# Report keys persisted as the per-cycle summary (drives history + totals).
_SUMMARY_KEYS = (
    "mode", "available_volume", "scanned", "candidates", "planned_buys",
    "planned_cost", "planned_profit", "planned_resell_profit",
    "planned_offers", "planned_offer_cost", "planned_offer_profit",
    "fills_ok", "order_states",
)


def _cycle_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {key: report.get(key) for key in _SUMMARY_KEYS}


def _report(cfg: TraderConfig, wallet: Wallet, sol_rate: float,
            sol_balance: float, usdc_balance: float, available_volume: float,
            listings: list, candidates: list, plan: BuyPlan,
            orders: list[Order], live: bool, demo: bool = False,
            near_misses: list | None = None,
            cycle_id: str = "") -> dict[str, Any]:
    # "Book" profit if everything sold at full insured value (upper bound).
    planned_profit = sum(c.market_usd - c.ask_usd for c in plan.items)
    offer_profit = sum(o.candidate.market_usd - o.offer_usd for o in plan.offers)
    # Realistic profit using the resale rule: relist below market value.
    resell_profit = plan.resell_profit
    offer_resell_profit = plan.offer_resell_profit
    mode = "DEMO" if demo else ("LIVE" if live else "DRY-RUN")
    # Orders that represent an external trading action (buys + offers); LIST
    # orders are the relist side and are reported separately/implicitly.
    action_orders = [o for o in orders
                     if o.kind in (OrderKind.BUY, OrderKind.OFFER)]
    return {
        "mode": mode,
        "demo": demo,
        "cycle_id": cycle_id,
        "wallet": wallet.address,
        "sol_rate": sol_rate,
        "sol_balance": sol_balance,
        "usdc_balance": usdc_balance,
        "available_volume": available_volume,
        "scanned": len(listings),
        "candidates": len(candidates),
        "escalated": plan.escalated,
        "card_cap_usd": plan.card_cap_usd,
        "direct_budget": plan.direct_budget,
        "offer_budget": plan.offer_budget,
        "resell_discount_pct": cfg.resell_discount_pct,
        "planned_buys": plan.count,
        "planned_cost": plan.total_cost,
        "planned_profit": planned_profit,
        "planned_resell_value": plan.resell_value,
        "planned_resell_profit": resell_profit,
        "planned_offers": plan.offer_count,
        "planned_offer_cost": plan.offer_cost,
        "planned_offer_profit": offer_profit,
        "planned_offer_resell_profit": offer_resell_profit,
        "remaining_volume": plan.remaining_volume,
        "skipped": plan.skipped,
        "fills_ok": sum(1 for o in action_orders if o.succeeded),
        "order_states": _order_states(orders),
        "near_misses": near_misses or [],
        "executed": [
            {
                "name": o.name,
                "nft": o.nft,
                "kind": o.kind.value,  # "buy" or "offer"
                "status": o.status.value,
                "price_usd": round(o.price_usd, 2),
                "market_usd": round(o.market_usd, 2),
                "resell_usd": round(o.resell_usd, 2),
                "ok": o.succeeded,
                "simulated": o.simulated,
                "detail": o.detail,
                "category": o.category,
            }
            for o in action_orders
        ],
        "items": [
            {
                "name": c.name,
                "nft": c.nft,
                "ask_usd": round(c.ask_usd, 2),
                "market_usd": round(c.market_usd, 2),
                "resell_usd": round(c.resell_usd, 2),
                "resell_profit": round(c.resell_usd - c.ask_usd, 2),
                "discount_pct": round(c.discount_pct, 1),
                "category": c.card.get("category", ""),
                "currency": c.card.get("currency", ""),
            }
            for c in plan.items
        ],
        "offers": [
            {
                "name": o.name,
                "nft": o.nft,
                "ask_usd": round(o.candidate.ask_usd, 2),
                "offer_usd": round(o.offer_usd, 2),
                "market_usd": round(o.candidate.market_usd, 2),
                "resell_usd": round(o.candidate.resell_usd, 2),
                "resell_profit": round(o.candidate.resell_usd - o.offer_usd, 2),
                "category": o.candidate.card.get("category", ""),
                "currency": o.candidate.card.get("currency", ""),
            }
            for o in plan.offers
        ],
    }
