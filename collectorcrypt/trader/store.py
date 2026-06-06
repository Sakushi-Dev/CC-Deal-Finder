"""Durable persistence for trader cycles and orders.

This is the trader's source of truth. Where the engine produces in-memory
:class:`~collectorcrypt.trader.orders.Order` objects, the :class:`OrderStore`
writes them to a local SQLite database so the full order lifecycle survives an
application restart and can be reconciled later (ETAPPE 5+).

Why SQLite (stdlib ``sqlite3``)
-------------------------------
* **Atomic, durable writes** — no half-written JSONL lines on a crash.
* **Idempotency by construction** — ``orders.client_order_id`` is ``UNIQUE``,
  so replaying or retrying a cycle can never insert the same intent twice.
* **Queryable state** — reconciliation needs "all active orders", "open
  offers", "relist candidates"; those are one indexed query each.
* **No new dependency** — ``sqlite3`` ships with Python.

Concurrency
-----------
The manager runs cycles in a worker thread while the Flask request thread reads
snapshots. Each public method opens its own short-lived connection (SQLite
handles file locking) and writes are additionally serialised with a process
lock, so reads never see a partially applied cycle.

Security
--------
The database holds **no secrets**: never a private key, never raw auth tokens.
Only public order economics, public nft/wallet addresses and external CC/tx
ids are stored. The file path defaults to ``trader_store.db`` and is covered by
the ``*.db`` rule in ``.gitignore``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .orders import ACTIVE_STATUSES, Order, OrderKind, OrderStatus

STORE_PATH = Path(os.environ.get("TRADER_STORE_PATH", "trader_store.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id      TEXT PRIMARY KEY,
    ts            REAL NOT NULL,
    mode          TEXT,
    wallet        TEXT,
    demo          INTEGER DEFAULT 0,
    config_json   TEXT,
    summary_json  TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    client_order_id TEXT UNIQUE,
    cycle_id        TEXT,
    parent_id       TEXT,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    nft             TEXT,
    name            TEXT,
    category        TEXT,
    currency        TEXT,
    price_usd       REAL DEFAULT 0,
    market_usd      REAL DEFAULT 0,
    resell_usd      REAL DEFAULT 0,
    simulated       INTEGER DEFAULT 1,
    external_id     TEXT,
    signature       TEXT,
    error           TEXT,
    detail          TEXT,
    created_at      REAL,
    updated_at      REAL,
    history_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_cycle ON orders(cycle_id);
CREATE INDEX IF NOT EXISTS idx_orders_kind_status ON orders(kind, status);
CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts);
CREATE TABLE IF NOT EXISTS runtime (
    key        TEXT PRIMARY KEY,
    value_json TEXT,
    updated_at REAL
);
"""

_ORDER_COLUMNS = (
    "id", "client_order_id", "cycle_id", "parent_id", "kind", "status",
    "nft", "name", "category", "currency", "price_usd", "market_usd",
    "resell_usd", "simulated", "external_id", "signature", "error", "detail",
    "created_at", "updated_at", "history_json",
)


class OrderStore:
    """SQLite-backed persistence for cycles and orders."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else STORE_PATH
        self._write_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------ #
    # Connection / schema
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL keeps readers (the UI snapshot) from blocking the cycle writer.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def save_cycle(self, cycle_id: str, *, mode: str, wallet: str, demo: bool,
                   config_snapshot: dict[str, Any],
                   summary: dict[str, Any]) -> None:
        """Persist (or replace) a cycle header with its config snapshot."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO cycles "
                "(cycle_id, ts, mode, wallet, demo, config_json, summary_json) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(cycle_id) DO UPDATE SET "
                "ts=excluded.ts, mode=excluded.mode, wallet=excluded.wallet, "
                "demo=excluded.demo, config_json=excluded.config_json, "
                "summary_json=excluded.summary_json",
                (cycle_id, time.time(), mode, wallet, 1 if demo else 0,
                 json.dumps(config_snapshot, ensure_ascii=False),
                 json.dumps(summary, ensure_ascii=False)),
            )

    def upsert_order(self, order: Order) -> None:
        """Insert a new order or update an existing one (idempotent).

        Conflicts are resolved on ``client_order_id`` so the same trading intent
        (cycle + kind + nft) is stored exactly once even across retries and
        restarts. The order's own ``id`` is preserved on first insert.
        """
        with self._write_lock, self._connect() as conn:
            self._upsert_order(conn, order)

    def save_orders(self, orders: list[Order]) -> None:
        """Persist many orders in a single transaction."""
        if not orders:
            return
        with self._write_lock, self._connect() as conn:
            for order in orders:
                self._upsert_order(conn, order)

    def _upsert_order(self, conn: sqlite3.Connection, order: Order) -> None:
        d = order.to_dict()
        row = (
            d["id"], d["client_order_id"], d["cycle_id"], d["parent_id"],
            d["kind"], d["status"], d["nft"], d["name"], d["category"],
            d["currency"], d["price_usd"], d["market_usd"], d["resell_usd"],
            1 if d["simulated"] else 0, d["external_id"], d["signature"],
            d["error"], d["detail"], d["created_at"], d["updated_at"],
            json.dumps(d["history"], ensure_ascii=False),
        )
        # Conflict may arise on the primary key (id) or the unique
        # client_order_id; update the lifecycle/reference columns either way.
        conn.execute(
            "INSERT INTO orders "
            "(id, client_order_id, cycle_id, parent_id, kind, status, nft, "
            " name, category, currency, price_usd, market_usd, resell_usd, "
            " simulated, external_id, signature, error, detail, created_at, "
            " updated_at, history_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_order_id) DO UPDATE SET "
            "status=excluded.status, parent_id=excluded.parent_id, "
            "external_id=excluded.external_id, signature=excluded.signature, "
            "error=excluded.error, detail=excluded.detail, "
            "updated_at=excluded.updated_at, history_json=excluded.history_json",
            row,
        )

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get_order(self, order_id: str) -> Order | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,))
            row = cur.fetchone()
        return _row_to_order(row) if row else None

    def get_by_client_order_id(self, client_order_id: str) -> Order | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM orders WHERE client_order_id=?",
                (client_order_id,),
            )
            row = cur.fetchone()
        return _row_to_order(row) if row else None

    def active_orders(self) -> list[Order]:
        """All orders still in flight (must be watched by reconciliation)."""
        return self._orders_where(
            "status IN (%s)" % ",".join("?" * len(ACTIVE_STATUSES)),
            tuple(s.value for s in ACTIVE_STATUSES),
        )

    def open_offers(self) -> list[Order]:
        return self._orders_where(
            "kind=? AND status=?", (OrderKind.OFFER.value, OrderStatus.OPEN.value)
        )

    def relist_candidates(self) -> list[Order]:
        return self._orders_where(
            "kind=? AND status=?", (OrderKind.LIST.value, OrderStatus.PLANNED.value)
        )

    def orders_for_cycle(self, cycle_id: str) -> list[Order]:
        return self._orders_where("cycle_id=?", (cycle_id,))

    def _orders_where(self, clause: str, params: tuple) -> list[Order]:
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT * FROM orders WHERE {clause} ORDER BY created_at",
                params,
            )
            rows = cur.fetchall()
        return [_row_to_order(r) for r in rows]

    def recent_cycles(self, limit: int = 200) -> list[dict[str, Any]]:
        """Cycle headers (newest last) for history reconstruction on restart."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT cycle_id, ts, mode, wallet, demo, summary_json "
                "FROM cycles ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in reversed(rows):  # oldest first, like the old history deque
            try:
                summary = json.loads(r["summary_json"] or "{}")
            except ValueError:
                summary = {}
            out.append({
                "cycle_id": r["cycle_id"],
                "ts": r["ts"],
                "mode": r["mode"],
                "wallet": r["wallet"],
                "demo": bool(r["demo"]),
                **summary,
            })
        return out

    def counts_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
            )
            rows = cur.fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ #
    # Risk usage queries (ETAPPE 7)
    # ------------------------------------------------------------------ #
    def confirmed_spend_since(self, since_ts: float) -> float:
        """Realized USD spend on confirmed, non-simulated buys/offers.

        Drives the rolling spend caps. Only ``CONFIRMED`` real orders count:
        a confirmed buy spent its ``price_usd`` and a confirmed (filled) offer
        likewise. Simulated (dry-run/demo) orders never count.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT COALESCE(SUM(price_usd), 0) AS spent FROM orders "
                "WHERE simulated=0 AND status=? AND kind IN (?, ?) "
                "AND created_at >= ?",
                (OrderStatus.CONFIRMED.value, OrderKind.BUY.value,
                 OrderKind.OFFER.value, float(since_ts)),
            )
            row = cur.fetchone()
        return float(row["spent"] or 0.0)

    def open_position_count(self) -> int:
        """Number of real, in-flight orders (active = must be watched)."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE simulated=0 "
                "AND status IN (%s)" % ",".join("?" * len(ACTIVE_STATUSES)),
                tuple(s.value for s in ACTIVE_STATUSES),
            )
            row = cur.fetchone()
        return int(row["n"] or 0)

    def recent_terminal_statuses(self, limit: int = 50) -> list[str]:
        """Newest-first statuses of recent real, settled orders.

        A "settled" order is one that left the in-flight set: ``CONFIRMED``,
        ``FAILED`` or ``CANCELLED``. Used by the consecutive-failure kill
        switch. Returns the status strings ordered newest first.
        """
        terminal = (OrderStatus.CONFIRMED.value, OrderStatus.FAILED.value,
                    OrderStatus.CANCELLED.value)
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT status FROM orders WHERE simulated=0 AND status IN (?, ?, ?) "
                "ORDER BY updated_at DESC LIMIT ?",
                (*terminal, int(limit)),
            )
            rows = cur.fetchall()
        return [r["status"] for r in rows]

    # ------------------------------------------------------------------ #
    # Runtime state (ETAPPE 8) — survives a restart/crash
    # ------------------------------------------------------------------ #
    def get_runtime(self, key: str, default: Any = None) -> Any:
        """Read a persisted runtime value (``default`` if absent/invalid)."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT value_json FROM runtime WHERE key=?", (key,)
            )
            row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (ValueError, TypeError):
            return default

    def set_runtime(self, key: str, value: Any) -> None:
        """Persist a small JSON-serialisable runtime value (idempotent upsert).

        Used for the loop control state (active/paused/interval) so the worker
        can resume after an application restart instead of sitting idle. Holds
        **no secrets** — only public control flags.
        """
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runtime (key, value_json, updated_at) "
                "VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )


def _row_to_order(row: sqlite3.Row) -> Order:
    try:
        history = json.loads(row["history_json"] or "[]")
    except ValueError:
        history = []
    return Order.from_dict({
        "id": row["id"],
        "client_order_id": row["client_order_id"],
        "cycle_id": row["cycle_id"],
        "parent_id": row["parent_id"],
        "kind": row["kind"],
        "status": row["status"],
        "nft": row["nft"],
        "name": row["name"],
        "category": row["category"],
        "currency": row["currency"],
        "price_usd": row["price_usd"],
        "market_usd": row["market_usd"],
        "resell_usd": row["resell_usd"],
        "simulated": bool(row["simulated"]),
        "external_id": row["external_id"],
        "signature": row["signature"],
        "error": row["error"],
        "detail": row["detail"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "history": history,
    })
