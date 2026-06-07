"""Trader domain model — orders and their lifecycle.

This module is the backbone of the trader's *real* state representation. Where
the engine used to produce throwaway report dictionaries, it now materialises a
:class:`BuyPlan` into typed :class:`Order` objects that carry an explicit
:class:`OrderStatus` through a controlled lifecycle.

The same :class:`Order` objects flow through **both** the dry-run and the live
pipeline — only the executor differs. Dry-run orders are flagged
``simulated=True`` and shortcut straight to a terminal state; the live executor
(ETAPPE 5) will walk them through the intermediate states
(``SUBMITTED → SIGNED → PENDING → CONFIRMED``) as it sends, signs and
broadcasts real transactions.

Design rules
------------
* **Terminal states are final.** Once an order is ``CONFIRMED``, ``FAILED`` or
  ``CANCELLED`` it can never transition again. This prevents a reconciliation
  bug from "reviving" a closed order.
* **Every transition is recorded.** :meth:`Order.transition` appends to an
  in-object audit trail so the full history is observable and (ETAPPE 2)
  persistable.
* **Idempotency first.** Each order gets a deterministic
  :attr:`Order.client_order_id` derived from ``(cycle_id, kind, nft)`` so the
  same intent is never submitted twice across restarts/retries.

This module has no I/O, no HTTP and no signing — it is pure domain logic and is
therefore trivially testable (ETAPPE 9).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .strategy import BuyPlan


class OrderKind(str, Enum):
    """What the order is trying to do."""

    BUY = "buy"      # direct purchase at the listing's ask price
    OFFER = "offer"  # standing bid placed below the ask
    LIST = "list"    # relist an owned card for sale (the exit/sell side)


class OrderStatus(str, Enum):
    """Where the order is in its lifecycle.

    The full live progression is::

        PLANNED → SUBMITTED → SIGNED → PENDING → CONFIRMED

    with ``OPEN`` as the resting state for an offer that sits on the order book
    and ``FAILED`` / ``CANCELLED`` as the alternative terminal outcomes. Dry-run
    orders shortcut directly from ``PLANNED`` to a terminal/active state.
    """

    PLANNED = "planned"      # decided by the strategy; nothing sent yet
    SUBMITTED = "submitted"  # request sent to CC; awaiting tx payload / ack
    SIGNED = "signed"        # transaction signed locally; not yet broadcast
    PENDING = "pending"      # broadcast/accepted; awaiting on-chain confirmation
    OPEN = "open"            # offer is resting on the book (active, not filled)
    CONFIRMED = "confirmed"  # buy filled / listing live / offer accepted+filled
    FAILED = "failed"        # execution failed (see ``error``)
    CANCELLED = "cancelled"  # withdrawn or superseded


# Orders in these states still need watching by the reconciliation loop.
ACTIVE_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.SUBMITTED, OrderStatus.SIGNED, OrderStatus.PENDING,
     OrderStatus.OPEN}
)

# Orders in these states are final and must never transition again.
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.CONFIRMED, OrderStatus.FAILED, OrderStatus.CANCELLED}
)

# Allowed forward transitions. ``PLANNED`` may shortcut to any later state so
# the dry-run executor can resolve an order in a single step; the live executor
# walks the intermediate states. Terminal states have no outgoing edges.
_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLANNED: frozenset({
        OrderStatus.SUBMITTED, OrderStatus.SIGNED, OrderStatus.PENDING,
        OrderStatus.OPEN, OrderStatus.CONFIRMED, OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    }),
    OrderStatus.SUBMITTED: frozenset({
        OrderStatus.SIGNED, OrderStatus.PENDING, OrderStatus.OPEN,
        OrderStatus.CONFIRMED, OrderStatus.FAILED, OrderStatus.CANCELLED,
    }),
    OrderStatus.SIGNED: frozenset({
        OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    }),
    OrderStatus.PENDING: frozenset({
        OrderStatus.OPEN, OrderStatus.CONFIRMED, OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    }),
    OrderStatus.OPEN: frozenset({
        OrderStatus.CONFIRMED, OrderStatus.FAILED, OrderStatus.CANCELLED,
    }),
    OrderStatus.CONFIRMED: frozenset(),
    OrderStatus.FAILED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


class OrderError(RuntimeError):
    """Raised on an illegal order-state transition."""


def make_client_order_id(cycle_id: str, kind: OrderKind, nft: str) -> str:
    """Deterministic idempotency key for an order.

    The same ``(cycle_id, kind, nft)`` always yields the same id, so a retried
    or replayed cycle cannot accidentally submit the same intent twice. The
    cycle id keeps it unique across cycles (the bot may legitimately re-buy the
    same nft in a later cycle).
    """
    return f"{cycle_id}:{kind.value}:{nft}"


@dataclass
class Order:
    """A single trading intent and its lifecycle state.

    The economic fields (``price_usd``, ``market_usd``, ``resell_usd``) are
    snapshotted from the strategy at planning time so the order is a complete,
    self-contained record even after the source listing disappears.
    """

    kind: OrderKind
    nft: str
    name: str = ""
    category: str = ""
    currency: str = ""
    status: OrderStatus = OrderStatus.PLANNED

    # Economics (snapshot at planning time).
    price_usd: float = 0.0       # what we pay (ask for buys, bid for offers,
                                 # list price for relists)
    market_usd: float = 0.0      # insured/market value reference
    resell_usd: float = 0.0      # intended relist price for the bought card

    simulated: bool = True       # True for dry-run/demo; False only for live

    # Identity / idempotency.
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cycle_id: str = ""
    client_order_id: str = ""
    parent_id: str = ""          # links a LIST order back to its BUY order

    # CC internal card identifier (raw card ``id``, e.g. "2024122019C5785").
    # Required by the verified ``marketplace/make-offer`` body; captured at
    # planning time so the offer can be submitted later from a self-contained
    # record.
    card_id: str = ""

    # External references, filled during execution / reconciliation.
    external_id: str = ""        # CC receipt / listing / offer id
    signature: str = ""          # Solana transaction signature
    error: str = ""
    detail: str = ""

    # Offer penetration (ETAPPE 1): how many times an open offer has been
    # bumped to re-trigger the owner's notification, and when last bumped.
    bump_count: int = 0
    last_bump_at: float = 0.0

    # Timestamps and audit trail.
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.client_order_id and self.cycle_id and self.nft:
            self.client_order_id = make_client_order_id(
                self.cycle_id, self.kind, self.nft
            )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def transition(self, status: OrderStatus, *, detail: str = "",
                   error: str = "", external_id: str = "",
                   signature: str = "") -> "Order":
        """Move the order to ``status``, recording the change.

        Raises :class:`OrderError` for any transition that the lifecycle does
        not allow (e.g. mutating a terminal order). The optional fields update
        the order's external references in the same atomic step.
        """
        if status not in _ALLOWED_TRANSITIONS.get(self.status, frozenset()):
            raise OrderError(
                f"Illegal order transition {self.status.value} -> "
                f"{status.value} (order {self.id}, kind {self.kind.value})."
            )
        prev = self.status
        self.status = status
        if detail:
            self.detail = detail
        if error:
            self.error = error
        if external_id:
            self.external_id = external_id
        if signature:
            self.signature = signature
        self.updated_at = time.time()
        self.history.append({
            "ts": self.updated_at,
            "from": prev.value,
            "to": status.value,
            "detail": detail,
            "error": error,
        })
        return self

    # ------------------------------------------------------------------ #
    # Predicates
    # ------------------------------------------------------------------ #
    @property
    def succeeded(self) -> bool:
        """A "good" outcome: a confirmed fill or a resting/open offer."""
        return self.status in (OrderStatus.CONFIRMED, OrderStatus.OPEN)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """Still in flight — reconciliation must keep watching it."""
        return self.status in ACTIVE_STATUSES

    @property
    def is_open_offer(self) -> bool:
        return self.kind is OrderKind.OFFER and self.status is OrderStatus.OPEN

    @property
    def is_relist_candidate(self) -> bool:
        return self.kind is OrderKind.LIST and self.status is OrderStatus.PLANNED

    # ------------------------------------------------------------------ #
    # Serialization (foundation for ETAPPE 2 persistence)
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cycle_id": self.cycle_id,
            "client_order_id": self.client_order_id,
            "parent_id": self.parent_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "nft": self.nft,
            "card_id": self.card_id,
            "name": self.name,
            "category": self.category,
            "currency": self.currency,
            "price_usd": self.price_usd,
            "market_usd": self.market_usd,
            "resell_usd": self.resell_usd,
            "simulated": self.simulated,
            "external_id": self.external_id,
            "signature": self.signature,
            "error": self.error,
            "detail": self.detail,
            "bump_count": self.bump_count,
            "last_bump_at": self.last_bump_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Order":
        order = cls(
            kind=OrderKind(data["kind"]),
            nft=data.get("nft", ""),
            name=data.get("name", ""),
            category=data.get("category", ""),
            currency=data.get("currency", ""),
            status=OrderStatus(data.get("status", OrderStatus.PLANNED.value)),
            price_usd=float(data.get("price_usd", 0.0)),
            market_usd=float(data.get("market_usd", 0.0)),
            resell_usd=float(data.get("resell_usd", 0.0)),
            simulated=bool(data.get("simulated", True)),
            id=data.get("id") or uuid.uuid4().hex,
            cycle_id=data.get("cycle_id", ""),
            client_order_id=data.get("client_order_id", ""),
            parent_id=data.get("parent_id", ""),
            card_id=data.get("card_id", ""),
            external_id=data.get("external_id", ""),
            signature=data.get("signature", ""),
            error=data.get("error", ""),
            detail=data.get("detail", ""),
            bump_count=int(data.get("bump_count") or 0),
            last_bump_at=float(data.get("last_bump_at") or 0.0),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            history=list(data.get("history", [])),
        )
        return order


def plan_to_orders(plan: BuyPlan, cycle_id: str, *,
                   simulated: bool) -> list[Order]:
    """Materialise a :class:`BuyPlan` into ``PLANNED`` orders.

    One order is created per direct buy and per offer. Relist (``LIST``) orders
    are **not** created here: a card can only be relisted once its buy has
    actually confirmed, so the executor creates the linked relist order as a
    follow-up of a confirmed buy.
    """
    orders: list[Order] = []
    for item in plan.items:
        orders.append(Order(
            kind=OrderKind.BUY,
            nft=item.nft,
            card_id=item.card.get("card_id", ""),
            name=item.name,
            category=item.card.get("category", ""),
            currency=item.card.get("currency", ""),
            price_usd=item.ask_usd,
            market_usd=item.market_usd,
            resell_usd=item.resell_usd,
            simulated=simulated,
            cycle_id=cycle_id,
        ))
    for offer in plan.offers:
        cand = offer.candidate
        orders.append(Order(
            kind=OrderKind.OFFER,
            nft=cand.nft,
            card_id=cand.card.get("card_id", ""),
            name=cand.name,
            category=cand.card.get("category", ""),
            currency=cand.card.get("currency", ""),
            price_usd=offer.offer_usd,
            market_usd=cand.market_usd,
            resell_usd=cand.resell_usd,
            simulated=simulated,
            cycle_id=cycle_id,
        ))
    return orders


def relist_order_for(buy: Order) -> Order:
    """Build the ``PLANNED`` relist order linked to a confirmed buy."""
    return Order(
        kind=OrderKind.LIST,
        nft=buy.nft,
        card_id=buy.card_id,
        name=buy.name,
        category=buy.category,
        currency=buy.currency,
        price_usd=buy.resell_usd,
        market_usd=buy.market_usd,
        resell_usd=buy.resell_usd,
        simulated=buy.simulated,
        cycle_id=buy.cycle_id,
        parent_id=buy.id,
    )
