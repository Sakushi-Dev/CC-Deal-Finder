"""Order execution.

Two executors share one interface and operate on the **same** typed
:class:`Order` objects produced by the planning pipeline — only the side
effects differ:

* :class:`DryRunExecutor` — resolves orders in-memory, spends nothing, touches
  no key. Buys/relists shortcut to ``CONFIRMED`` and offers to ``OPEN``, all
  flagged ``simulated``. This is the default and what we run until the full
  live flow is verified.
* :class:`LiveExecutor` — the only component that spends real funds. It drives
  the complete CollectorCrypt flow per order: preflight -> initiate (buy/offer)
  -> receive an unsigned transaction -> sign locally -> broadcast -> interpret
  the result -> persist every state transition. It is reached **only** when the
  engine confirms live mode is fully armed (master switch + signing wallet +
  real auth provider), so live spending cannot happen by accident.

Because both executors consume and return ``list[Order]``, the engine,
persistence (ETAPPE 2) and reconciliation see a single, uniform state
representation regardless of mode.

Robustness rules for the live path (no fire-and-forget)
-------------------------------------------------------
* **Preflight before every send**: a signing wallet, sufficient remaining
  budget, no duplicate of an already-submitted intent, and sane price/market
  assumptions. A failed precondition fails *that* order and moves on; it never
  aborts the whole batch and never spends.
* **Explicit state machine**: ``PLANNED -> SUBMITTED -> SIGNED -> PENDING ->
  CONFIRMED`` (or ``OPEN`` for a resting offer), with ``FAILED`` as the safe
  terminal outcome on any error.
* **Persist after each transition** so an interruption leaves a durable,
  reconcilable trail instead of a lost in-flight order.
* **Never auto-retry a write**: the trading client does not retry state-
  changing calls, and a transient failure here marks the order ``FAILED`` for
  the reconciler/operator rather than risking a double-spend.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from .ccapi import (CCApiError, CCAuthError, CCTradingClient)
from .orders import Order, OrderKind, OrderStatus, relist_order_for
from .store import (Holding, HOLDING_HELD, HOLDING_LISTED, HOLDING_SOLD)
from .wallet import WalletError

logger = logging.getLogger("collectorcrypt.trader.executor")


class Executor(Protocol):
    def execute(self, orders: list[Order]) -> list[Order]: ...


class DryRunExecutor:
    """Resolves planned orders in-memory. Spends nothing, signs nothing.

    Returns the input orders (transitioned to their resolved state) plus a
    linked, confirmed relist order for every confirmed buy that has a positive
    resale price — mirroring the real exit flow without any side effects.
    """

    def execute(self, orders: list[Order]) -> list[Order]:
        result: list[Order] = []
        for order in orders:
            if order.kind is OrderKind.BUY:
                order.transition(OrderStatus.CONFIRMED,
                                 detail="dry-run: no transaction sent")
                result.append(order)
                # Sell rule: immediately relist the bought card below market.
                if order.resell_usd > 0:
                    relist = relist_order_for(order)
                    relist.transition(OrderStatus.CONFIRMED,
                                      detail="dry-run: would relist for sale")
                    result.append(relist)
            elif order.kind is OrderKind.OFFER:
                order.transition(OrderStatus.OPEN,
                                 detail="dry-run: no offer sent")
                result.append(order)
            else:  # an already-built LIST order
                order.transition(OrderStatus.CONFIRMED,
                                 detail="dry-run: would relist for sale")
                result.append(order)
        return result

    # ------------------------------------------------------------------ #
    # Maintenance simulations (ETAPPE 6) — order transitions, no side effects
    # ------------------------------------------------------------------ #
    # These mirror the live maintenance actions (offer bump/cancel, listing
    # markdown, offer accept) as pure in-memory transitions so the lifecycle
    # shapes are testable without any client/wallet/store. The live executor's
    # equivalents stay safe-failure until ETAPPE 8 verifies the request shapes.
    def bump_offer(self, order: Order, new_price: float, *,
                   now: float | None = None) -> Order:
        """Simulate raising an open offer's bid (offer penetration)."""
        order.price_usd = float(new_price)
        order.bump_count += 1
        order.last_bump_at = time.time() if now is None else float(now)
        order.detail = "dry-run: bumped offer (no tx sent)"
        return order

    def cancel_offer(self, order: Order) -> Order:
        """Simulate cancelling an exhausted offer (escrow would refund)."""
        order.transition(OrderStatus.CANCELLED,
                         detail="dry-run: cancelled offer")
        return order

    def markdown_listing(self, order: Order, new_price: float) -> Order:
        """Simulate marking down a live listing's price."""
        order.price_usd = float(new_price)
        order.detail = "dry-run: marked down listing (no tx sent)"
        return order

    def accept_offer(self, order: Order, buyer: str, price: float) -> Order:
        """Simulate accepting an incoming offer (the card sells)."""
        order.price_usd = float(price)
        order.transition(
            OrderStatus.CONFIRMED,
            detail=f"dry-run: accepted offer of {float(price)} from {buyer}")
        return order



class LiveExecutor:
    """Real on-chain purchases/offers via the CollectorCrypt trading flow.

    Reached only when the engine has confirmed live mode is fully armed. Each
    order is taken through preflight -> initiate -> sign -> broadcast -> result,
    with every transition persisted. State-changing calls are never retried; any
    error fails that single order safely (``FAILED``) and the batch continues.
    """

    def __init__(self, wallet, rpc_url: str, *,
                 session_provider=None,
                 client: CCTradingClient | None = None,
                 store=None,
                 available_volume: float = 0.0,
                 cfg=None,
                 ledger=None) -> None:
        self._wallet = wallet
        self._rpc_url = rpc_url
        self._session_provider = session_provider
        # The authenticated trading client. Built from the session provider if
        # one is not injected (tests inject a fake).
        self._client = client or CCTradingClient(
            session_provider=session_provider
        )
        self._store = store
        self._cfg = cfg
        # Append-only transaction ledger (provable trade history). Optional; a
        # money event is recorded only when one is wired in. Never raises.
        self._ledger = ledger
        # Budget envelope for this cycle. Each confirmed buy / opened offer
        # decrements it; an order that would exceed it is failed (a safety net
        # on top of the planner, which already sizes within the budget).
        self._remaining = max(0.0, float(available_volume))

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def execute(self, orders: list[Order]) -> list[Order]:
        # Global preflight: a live executor must be able to sign. If not, fail
        # everything safely rather than sending anything.
        if not getattr(self._wallet, "can_sign", False):
            for order in orders:
                self._fail(order, "live executor without a signing wallet")
            return list(orders)

        result: list[Order] = []
        for order in orders:
            try:
                if order.kind is OrderKind.BUY:
                    result.extend(self._execute_buy(order))
                elif order.kind is OrderKind.OFFER:
                    result.append(self._execute_offer(order))
                else:  # LIST orders are the exit side (ETAPPE 6); not sent here.
                    self._defer_relist(order)
                    result.append(order)
            except Exception as exc:  # noqa: BLE001 - one bad order must not abort the batch
                self._fail(order, f"unexpected error: {exc}")
                result.append(order)
        return result

    # ------------------------------------------------------------------ #
    # Buy
    # ------------------------------------------------------------------ #
    def _execute_buy(self, order: Order) -> list[Order]:
        if not self._preflight(order, order.price_usd):
            return [order]

        # 1) Initiate: ask CC for the unsigned purchase transaction.
        order.transition(OrderStatus.SUBMITTED, detail="initiating buy")
        self._persist(order)
        resp = self._client.initiate_buy(
            nft=order.nft, price=order.price_usd,
            wallet=self._wallet.address,
            currency=order.currency or "USDC",
        )
        tx = _extract_tx(resp)
        external_id = _extract_external_id(resp)
        if not tx:
            self._fail(order, "buy initiation returned no transaction to sign")
            return [order]

        # 2) Sign locally + 3) broadcast.
        if not self._sign_and_broadcast(order, tx, external_id):
            return [order]

        # 4) Spend accounting + holdings + relist follow-up for a confirmed buy.
        followups: list[Order] = []
        if order.status is OrderStatus.CONFIRMED:
            self._remaining = max(0.0, self._remaining - order.price_usd)
            self._record_txn(order, "buy")
            self._record_acquisition(order)
            if order.resell_usd > 0:
                relist = relist_order_for(order)
                self._defer_relist(relist)
                followups.append(relist)
        return [order, *followups]

    # ------------------------------------------------------------------ #
    # Offer
    # ------------------------------------------------------------------ #
    def _execute_offer(self, order: Order) -> Order:
        if not self._preflight(order, order.price_usd):
            return order

        # The verified make-offer body requires the card's internal CC id; an
        # offer without it would be rejected with 400, so fail safely (no spend,
        # no broadcast) before touching the client.
        if not order.card_id:
            self._fail(order, "offer missing card_id (cannot build make-offer)")
            return order

        order.transition(OrderStatus.SUBMITTED, detail="submitting offer")
        self._persist(order)
        resp = self._client.make_offer(
            nft=order.nft, card_id=order.card_id, price=order.price_usd,
            wallet=self._wallet.address, currency=order.currency or "USDC",
        )
        tx = _extract_tx(resp)
        external_id = _extract_external_id(resp)
        if not tx:
            self._fail(order, "offer submission returned no transaction to sign")
            return order

        # An offer settles into the resting OPEN state rather than CONFIRMED:
        # the bid sits on the book until the seller accepts it.
        if not self._sign_and_broadcast(order, tx, external_id,
                                        resting=True):
            return order
        if order.status is OrderStatus.OPEN:
            # Reserve the committed amount so we do not over-commit the budget.
            self._remaining = max(0.0, self._remaining - order.price_usd)
            self._record_txn(order, "offer_placed")
        elif order.status is OrderStatus.CONFIRMED:
            # An offer that filled on submit is a real acquisition + spend.
            self._remaining = max(0.0, self._remaining - order.price_usd)
            self._record_txn(order, "offer_filled")
            self._record_acquisition(order)
        return order

    # ------------------------------------------------------------------ #
    # Relist (exit / sell side — ETAPPE 6)
    # ------------------------------------------------------------------ #
    def relist(self, order: Order) -> Order:
        """Drive a single ``PLANNED`` relist (``LIST``) order onto the market.

        This is the live exit flow: a card bought (and confirmed) earlier is
        listed for sale at its planned resale price. It mirrors the buy/offer
        flow — prepare -> sign -> broadcast — with a sell-side preflight (no
        budget check; selling does not spend the USDC volume). Never raises;
        any failure marks the order ``FAILED`` for the operator/reconciler.
        """
        if order.kind is not OrderKind.LIST:
            self._fail(order, "relist() called on a non-LIST order")
            return order
        if not getattr(self._wallet, "can_sign", False):
            self._fail(order, "live executor without a signing wallet")
            return order
        try:
            if not self._preflight_relist(order):
                return order
            order.transition(OrderStatus.SUBMITTED, detail="creating listing")
            self._persist(order)
            resp = self._client.create_listing(
                nft=order.nft, price=order.price_usd,
                currency=order.currency or "USDC",
            )
            tx = _extract_tx(resp)
            external_id = _extract_external_id(resp)
            if not tx:
                self._fail(order,
                           "listing creation returned no transaction to sign")
                return order
            self._sign_and_broadcast(order, tx, external_id,
                                     confirm_detail="listing is live")
            if order.status is OrderStatus.CONFIRMED:
                self._record_txn(order, "listed")
                self._record_listed(order)
            return order
        except Exception as exc:  # noqa: BLE001 - one bad relist must not abort the batch
            self._fail(order, f"unexpected error: {exc}")
            return order

    # ------------------------------------------------------------------ #
    # Maintenance (ETAPPE 6/8)
    # ------------------------------------------------------------------ #
    # All four maintenance actions are LIVE (ETAPPE 8): their request shapes are
    # verified from DevTools captures — offer bump/cancel (update-offer /
    # cancel-offer) and listing markdown / offer accept (update-listing /
    # accept-offer). Offer bump/cancel act on a resting OPEN offer (no order
    # transition); markdown/accept act on a transient LIST order taken straight
    # to CONFIRMED on success so the engine can persist the result.
    def bump_offer(self, order: Order, new_price: float, *,
                   now: float | None = None) -> Order:
        """Raise an open offer's bid via the verified ``update-offer`` endpoint.

        ``update-offer`` returns a fresh transaction to sign + broadcast. A
        resting OPEN offer must NOT pass through ``SIGNED`` (the state machine
        forbids ``OPEN -> SIGNED``), so this uses a raw sign+broadcast that
        performs no order transition: on success the offer stays OPEN at the
        higher price with an incremented bump count; on any failure it stays
        OPEN at the old price for a later retry. Never raises.
        """
        if not getattr(self._wallet, "can_sign", False):
            order.detail = "offer bump skipped: live executor cannot sign"
            return order
        try:
            resp = self._client.update_offer(
                nft=order.nft, price=float(new_price),
                wallet=self._wallet.address,
                currency=order.currency or "USDC",
            )
        except CCApiError as exc:
            order.detail = f"offer bump failed: {exc}"
            logger.info("Live offer bump failed (nft=%s): %s", order.nft, exc)
            return order
        tx = _extract_tx(resp)
        if not tx:
            order.detail = "offer bump returned no transaction to sign"
            return order
        ok, signature = self._raw_sign_broadcast(order, tx, action="offer bump")
        if not ok:
            return order
        order.price_usd = float(new_price)
        order.bump_count += 1
        order.last_bump_at = time.time() if now is None else float(now)
        if signature:
            order.signature = signature
        order.detail = f"bumped offer to {order.price_usd}"
        self._persist(order)
        self._record_txn(order, "offer_bumped")
        return order

    def cancel_offer(self, order: Order) -> Order:
        """Withdraw an open offer via the verified ``cancel-offer`` endpoint.

        ``cancel-offer`` returns a transaction to sign + broadcast; on success
        the order transitions ``OPEN -> CANCELLED`` (the escrowed funds refund).
        On any failure the offer is left OPEN for a later attempt. Never raises.
        """
        if not getattr(self._wallet, "can_sign", False):
            order.detail = "offer cancel skipped: live executor cannot sign"
            return order
        try:
            resp = self._client.cancel_offer(
                nft=order.nft, wallet=self._wallet.address,
                currency=order.currency or "USDC",
            )
        except CCApiError as exc:
            order.detail = f"offer cancel failed: {exc}"
            logger.info("Live offer cancel failed (nft=%s): %s", order.nft, exc)
            return order
        tx = _extract_tx(resp)
        if not tx:
            order.detail = "offer cancel returned no transaction to sign"
            return order
        ok, signature = self._raw_sign_broadcast(order, tx, action="offer cancel")
        if not ok:
            return order
        order.transition(OrderStatus.CANCELLED,
                         detail="cancelled offer (escrow refunded)",
                         signature=signature or order.signature)
        self._persist(order)
        self._record_txn(order, "offer_cancelled")
        return order

    def markdown_listing(self, order: Order, new_price: float) -> Order:
        """Lower a live listing's price via the verified ``update-listing`` call.

        ``update-listing`` returns a fresh transaction to sign + broadcast that
        re-prices the existing listing in place. The subject is a transient
        ``LIST`` order (built by the engine, never persisted by this pass), so
        on success it is taken straight ``PLANNED -> CONFIRMED`` to signal the
        markdown settled; the engine then persists the new price on the holding.
        On any failure the order is left untouched (the listing keeps its old
        price) for a later attempt. Never raises.
        """
        if not getattr(self._wallet, "can_sign", False):
            order.detail = "listing markdown skipped: live executor cannot sign"
            return order
        try:
            resp = self._client.update_listing(
                nft=order.nft, price=float(new_price),
                wallet=self._wallet.address,
                currency=order.currency or "USDC",
            )
        except CCApiError as exc:
            order.detail = f"listing markdown failed: {exc}"
            logger.info("Live listing markdown failed (nft=%s): %s",
                        order.nft, exc)
            return order
        tx = _extract_tx(resp)
        if not tx:
            order.detail = "listing markdown returned no transaction to sign"
            return order
        ok, signature = self._raw_sign_broadcast(order, tx,
                                                 action="listing markdown")
        if not ok:
            return order
        order.price_usd = float(new_price)
        order.transition(OrderStatus.CONFIRMED,
                         detail=f"marked down to {order.price_usd}",
                         signature=signature or order.signature)
        self._record_txn(order, "markdown")
        return order

    def accept_offer(self, order: Order, buyer: str, price: float) -> Order:
        """Accept an incoming bid via the verified ``accept-offer`` call.

        An offer is referenced by ``buyer`` + ``price`` + ``nftAddress`` (there
        is no offer id). ``accept-offer`` returns a transaction to sign +
        broadcast that settles the sale. The subject is a transient ``LIST``
        order, so on success it is taken ``PLANNED -> CONFIRMED`` to signal the
        sale settled; the engine then marks the holding sold. A missing/invalid
        offer or any failure leaves the order untouched. Never raises.
        """
        if not getattr(self._wallet, "can_sign", False):
            order.detail = "offer accept skipped: live executor cannot sign"
            return order
        if not buyer or float(price) <= 0:
            order.detail = "offer accept skipped: no valid incoming offer"
            return order
        try:
            resp = self._client.accept_offer(
                nft=order.nft, buyer=buyer, price=float(price),
                wallet=self._wallet.address,
                currency=order.currency or "USDC",
            )
        except CCApiError as exc:
            order.detail = f"offer accept failed: {exc}"
            logger.info("Live offer accept failed (nft=%s): %s", order.nft, exc)
            return order
        tx = _extract_tx(resp)
        if not tx:
            order.detail = "offer accept returned no transaction to sign"
            return order
        ok, signature = self._raw_sign_broadcast(order, tx,
                                                 action="offer accept")
        if not ok:
            return order
        order.price_usd = float(price)
        order.transition(
            OrderStatus.CONFIRMED,
            detail=f"accepted offer of {order.price_usd} from {buyer}",
            signature=signature or order.signature)
        self._record_txn(order, "offer_accepted")
        return order

    def _raw_sign_broadcast(self, order: Order, tx: str, *,
                            action: str) -> tuple[bool, str]:
        """Sign ``tx`` and broadcast it WITHOUT any order transition.

        Used for maintenance on a resting OPEN offer (bump/cancel) where the
        state machine forbids the ``OPEN -> SIGNED`` step that
        :meth:`_sign_and_broadcast` relies on. Returns ``(ok, signature)``;
        ``ok`` is False on a signing or broadcast error (the caller then leaves
        the order in place). Broadcasts are never auto-retried. Never raises.
        """
        try:
            signed = self._wallet.sign_transaction(tx)
        except WalletError as exc:
            order.detail = f"{action} signing failed: {exc}"
            logger.info("Live %s signing failed (nft=%s): %s",
                        action, order.nft, exc)
            return False, ""
        try:
            resp = self._client.broadcast(
                signed_tx=signed, wallet=self._wallet.address, nft=order.nft)
        except CCApiError as exc:
            order.detail = f"{action} broadcast failed: {exc}"
            logger.info("Live %s broadcast failed (nft=%s): %s",
                        action, order.nft, exc)
            return False, ""
        return True, _extract_signature(resp)

    # ------------------------------------------------------------------ #
    # Shared sign + broadcast
    # ------------------------------------------------------------------ #
    def _sign_and_broadcast(self, order: Order, tx: str, external_id: str,
                            *, resting: bool = False,
                            confirm_detail: str = "purchase confirmed") -> bool:
        """Sign ``tx`` locally and broadcast it; advance ``order`` accordingly.

        Returns ``True`` if the order reached a good state (``CONFIRMED`` or, for
        a resting offer, ``OPEN``); ``False`` if it was failed. Never raises.
        """
        try:
            signed = self._wallet.sign_transaction(tx)
        except WalletError as exc:
            self._fail(order, f"signing failed: {exc}")
            return False
        order.transition(OrderStatus.SIGNED, detail="signed locally",
                         external_id=external_id)
        self._persist(order)

        try:
            resp = self._client.broadcast(
                signed_tx=signed, wallet=self._wallet.address, nft=order.nft)
        except CCApiError as exc:
            # Writes are never auto-retried; surface for the reconciler.
            self._fail(order, f"broadcast failed: {exc}")
            return False

        signature = _extract_signature(resp)
        order.transition(OrderStatus.PENDING, detail="broadcast", signature=signature)
        self._persist(order)

        confirmed = _is_confirmed(resp)
        if resting:
            # A resting offer is "successful" once accepted onto the book; if
            # the broadcast already reports settlement, treat it as confirmed.
            target = OrderStatus.CONFIRMED if _is_filled(resp) else OrderStatus.OPEN
            order.transition(target, detail="offer accepted onto the book")
            self._persist(order)
            return True
        if confirmed:
            order.transition(OrderStatus.CONFIRMED, detail=confirm_detail)
            self._persist(order)
            return True
        # Broadcast accepted but not yet confirmed on-chain: leave PENDING for
        # the reconciler to resolve. This is a successful send, not a failure.
        return True

    # ------------------------------------------------------------------ #
    # Preflight + helpers
    # ------------------------------------------------------------------ #
    def _preflight_relist(self, order: Order) -> bool:
        """Validate a relist just before sending. Fails it safely on any issue.

        Unlike a buy/offer there is no budget check (listing a card we already
        own does not spend the USDC volume). We require a sane, positive resale
        price and guard against re-listing an order that already advanced.
        """
        if self._store is not None and order.client_order_id:
            try:
                existing = self._store.get_by_client_order_id(
                    order.client_order_id)
            except Exception:  # noqa: BLE001 - a store hiccup must not block the decision
                existing = None
            # The relist candidate IS itself a persisted PLANNED order, so only
            # reject when a *different* record already carries this idempotency
            # key past PLANNED (a concurrent/duplicate listing attempt).
            if (existing is not None and existing.id != order.id
                    and existing.status is not OrderStatus.PLANNED):
                self._fail(
                    order,
                    f"listing already at '{existing.status.value}'; skipped "
                    "to avoid a duplicate listing",
                )
                return False
        if order.price_usd <= 0:
            self._fail(order, "no resale price; refusing to list")
            return False
        return True

    def _preflight(self, order: Order, cost: float) -> bool:
        """Validate an order just before sending. Fails it safely on any issue."""
        # Duplicate guard: a persisted order with the same idempotency key that
        # is already past PLANNED must not be re-sent (protects cycle replays).
        if self._store is not None and order.client_order_id:
            try:
                existing = self._store.get_by_client_order_id(
                    order.client_order_id)
            except Exception:  # noqa: BLE001 - a store hiccup must not block trading decisions
                existing = None
            if existing is not None and existing.status is not OrderStatus.PLANNED:
                self._fail(
                    order,
                    f"duplicate intent already at '{existing.status.value}'; "
                    "skipped to avoid double-submit",
                )
                return False

        # Budget guard: never commit more than the remaining cycle budget.
        if cost > self._remaining + 1e-9:
            self._fail(
                order,
                f"insufficient remaining budget (need {cost:.2f}, "
                f"have {self._remaining:.2f})",
            )
            return False

        # Price/market sanity: refuse to pay at/above market value (the whole
        # thesis is buying below market). market_usd <= 0 means no reference.
        if order.market_usd <= 0:
            self._fail(order, "no market reference; refusing to trade blind")
            return False
        if cost >= order.market_usd:
            self._fail(
                order,
                f"price {cost:.2f} >= market {order.market_usd:.2f}; "
                "refusing a non-discounted trade",
            )
            return False
        return True

    def _defer_relist(self, order: Order) -> None:
        """Persist a relist order as a PLANNED candidate for the exit flow.

        ETAPPE 5 covers buys and offers; the live relisting/exit flow is
        ETAPPE 6. The linked LIST order is recorded as a relist candidate so the
        exit flow (and reconciliation) can pick it up — it is never sent here.
        """
        order.detail = "relist candidate (live exit flow pending — ETAPPE 6)"
        self._persist(order)

    def _fail(self, order: Order, reason: str) -> None:
        if order.is_terminal:
            return
        logger.warning("Live order failed: %s (nft=%s)", reason, order.nft)
        order.transition(OrderStatus.FAILED, error=reason, detail=reason)
        self._persist(order)

    def _persist(self, order: Order) -> None:
        if self._store is None:
            return
        try:
            self._store.upsert_order(order)
        except Exception as exc:  # noqa: BLE001 - persistence failure must not abort a live send
            logger.error("Failed to persist order %s: %s", order.id, exc)

    # ------------------------------------------------------------------ #
    # Audit trail — provable transaction record + activity log
    # ------------------------------------------------------------------ #
    def _record_txn(self, order: Order, event: str) -> None:
        """Log a money event and append it to the transaction ledger.

        Both are best-effort and only for real (non-simulated) orders. The INFO
        log feeds the bot activity log; the ledger feeds the provable CSV trade
        history. Neither may ever abort a live send.
        """
        logger.info(
            "%s: %s '%s' %.2f %s (nft=%s, sig=%s)",
            event, order.kind.value, order.name or "?", order.price_usd,
            order.currency or "USDC", order.nft, order.signature or "-")
        if self._ledger is None:
            return
        try:
            self._ledger.record_order(order, event=event)
        except Exception as exc:  # noqa: BLE001 - a ledger write must never abort a live send
            logger.error("Failed to record transaction for %s: %s",
                         order.nft, exc)

    # ------------------------------------------------------------------ #
    # Holdings lifecycle (ETAPPE 5) — best-effort, never aborts a send
    # ------------------------------------------------------------------ #
    def _record_acquisition(self, order: Order) -> None:
        """Record the cost-basis holding for a settled buy/filled offer."""
        try:
            record_acquisition(self._store, order)
        except Exception as exc:  # noqa: BLE001 - a holdings write must never abort a live send
            logger.error("Failed to record holding for %s: %s",
                         order.nft, exc)

    def _record_listed(self, order: Order) -> None:
        """Flip the holding to ``listed`` when its relist goes live."""
        try:
            record_listed(self._store, order)
        except Exception as exc:  # noqa: BLE001 - a holdings write must never abort a relist
            logger.error("Failed to mark holding listed for %s: %s",
                         order.nft, exc)


# --------------------------------------------------------------------------- #
# Response interpretation (ASSUMED shapes — see docs/api.md)
# --------------------------------------------------------------------------- #
# The CollectorCrypt trading responses are reverse-engineered. These helpers
# read the expected fields defensively across plausible key names so a minor
# shape difference degrades to a clear failure rather than a wrong trade.
def _first(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    # Look one level into a common "data"/"result" envelope.
    for env in ("data", "result"):
        inner = data.get(env)
        if isinstance(inner, dict):
            for key in keys:
                if key in inner and inner[key] not in (None, ""):
                    return inner[key]
    return None


def _extract_tx(resp: Any) -> str:
    val = _first(resp, "transaction", "tx", "unsignedTransaction",
                 "serializedTransaction", "txData", "encodedTransaction")
    if val:
        return str(val)
    # VERIFIED: marketplace/buy returns a bare base64 transaction string, which
    # the transport wraps as ``{"data": "<base64>"}``. Accept that shape too.
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, str) and data:
        return data
    return ""


def _extract_external_id(resp: Any) -> str:
    val = _first(resp, "receiptId", "receipt_id", "id", "offerId", "offer_id",
                 "listingId", "listing_id", "orderId")
    return str(val) if val else ""


def _extract_signature(resp: Any) -> str:
    val = _first(resp, "signature", "txSignature", "txid", "txId",
                 "transactionSignature")
    return str(val) if val else ""


def _is_confirmed(resp: Any) -> bool:
    status = _first(resp, "status", "state", "result")
    if isinstance(status, str) and status.lower() in (
            "confirmed", "finalized", "success", "succeeded", "ok", "complete",
            "completed"):
        return True
    flag = _first(resp, "confirmed", "success", "finalized")
    return bool(flag)


def _is_filled(resp: Any) -> bool:
    status = _first(resp, "status", "state")
    if isinstance(status, str) and status.lower() in (
            "filled", "accepted", "sold", "matched"):
        return True
    return bool(_first(resp, "filled", "accepted"))


def _is_cancelled(resp: Any) -> bool:
    status = _first(resp, "status", "state")
    if isinstance(status, str) and status.lower() in (
            "cancelled", "canceled", "withdrawn", "expired", "rejected",
            "removed", "delisted"):
        return True
    return bool(_first(resp, "cancelled", "canceled", "withdrawn"))


# --------------------------------------------------------------------------- #
# Holdings lifecycle writers (ETAPPE 5)
# --------------------------------------------------------------------------- #
# Side-effect-free except for the holdings upsert. Shared by the live executor
# (settle time) and the status syncer (a fill/sale discovered later), so both
# entry points populate the same source of truth identically.
#
# Hard guard: a holding is **only ever** written for a *real* order
# (``simulated is False``). Dry-run/demo orders may simulate their transitions
# for tests but must never leave a row in the live holdings ledger.
def record_acquisition(store: Any, order: Order, *,
                       now: float | None = None) -> bool:
    """Write/refresh the ``held`` holdings row for a settled buy/filled offer.

    Captures the permanent cost basis (``acquired_at``/``cost_usd``/
    ``market_usd_at_buy``) the moment a BUY confirms or an OFFER fills. Returns
    ``True`` when a holding was written, ``False`` when a guard short-circuits
    (no store, simulated order, wrong kind, or missing nft).

    Idempotent: ``upsert_holding`` keeps ``acquired_at``/``cost_usd`` immutable
    on a repeat, so a fill seen twice (executor + later status sync) is safe.
    """
    if store is None or order is None or order.simulated:
        return False
    if order.kind not in (OrderKind.BUY, OrderKind.OFFER) or not order.nft:
        return False
    ts = time.time() if now is None else float(now)
    store.upsert_holding(Holding(
        nft=order.nft,
        name=order.name,
        category=order.category,
        acquired_at=ts,
        cost_usd=order.price_usd,
        market_usd_at_buy=order.market_usd,
        status=HOLDING_HELD,
    ))
    return True


def record_listed(store: Any, order: Order, *,
                  now: float | None = None) -> bool:
    """Flip the holding to ``listed`` when its relist (``LIST``) goes live.

    Sets ``listed_at``/``list_price_usd`` and ``status="listed"`` on the
    existing holding for ``order.nft``. Never invents a cost basis: if no
    holding exists yet (the buy was never recorded) it does nothing. Real
    ``LIST`` orders only.
    """
    if store is None or order is None or order.simulated:
        return False
    if order.kind is not OrderKind.LIST or not order.nft:
        return False
    holding = store.get_holding(order.nft)
    if holding is None:
        return False
    holding.listed_at = time.time() if now is None else float(now)
    holding.list_price_usd = order.price_usd
    holding.status = HOLDING_LISTED
    store.upsert_holding(holding)
    return True


def record_markdown(store: Any, holding: Any, new_price: float, *,
                    now: float | None = None) -> bool:
    """Persist a confirmed live markdown step on the holding.

    Lowers ``list_price_usd`` to ``new_price``, advances the markdown clock
    (``last_markdown_at``) and the step counter (``markdown_steps``) so the
    curve progresses and the inter-step interval is respected. The card stays
    ``listed``. Best-effort: does nothing when no holding/store is given.
    """
    if store is None or holding is None or not holding.nft:
        return False
    holding.list_price_usd = float(new_price)
    holding.markdown_steps = int(holding.markdown_steps or 0) + 1
    holding.last_markdown_at = time.time() if now is None else float(now)
    holding.status = HOLDING_LISTED
    store.upsert_holding(holding)
    return True


def record_sold(store: Any, order: Order, *,
                now: float | None = None) -> bool:
    """Mark the holding ``sold`` when its relist (``LIST``) order confirms.

    Sets ``sold_at`` and ``status="sold"`` on the existing holding for
    ``order.nft``. Does nothing when no holding exists. Real ``LIST`` orders
    only.
    """
    if store is None or order is None or order.simulated:
        return False
    if order.kind is not OrderKind.LIST or not order.nft:
        return False
    holding = store.get_holding(order.nft)
    if holding is None:
        return False
    holding.sold_at = time.time() if now is None else float(now)
    holding.status = HOLDING_SOLD
    store.upsert_holding(holding)
    return True


def record_sold_holding(store: Any, holding: Any, *,
                        now: float | None = None) -> bool:
    """Mark a known holding ``sold`` from the authoritative ownership reconcile.

    Unlike :func:`record_sold` (which keys off a confirming ``LIST`` order),
    this takes the holding directly — used when the owned-cards endpoint shows
    the NFT has left the wallet (sold or transferred away). Idempotent: skips a
    holding that is already sold or missing its nft.
    """
    if store is None or holding is None or not holding.nft:
        return False
    if holding.sold_at is not None:
        return False
    holding.sold_at = time.time() if now is None else float(now)
    holding.status = HOLDING_SOLD
    store.upsert_holding(holding)
    return True
