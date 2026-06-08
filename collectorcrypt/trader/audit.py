"""Operational audit trail: a transaction ledger and a bot activity log.

Two independent records that an operator needs once real money moves:

* :class:`TransactionLedger` — an append-only **CSV** of every real money
  event the bot performs (a card bought/offered/sold/listed/cancelled, when, how
  much, in which currency, with the on-chain signature). It lives in its own
  directory (default ``records/``) and is the provable, human- and
  spreadsheet-friendly trade history — the kind of evidence a tax authority may
  ask for. The on-chain ``signature`` lets anyone re-derive the exact network
  fee from a block explorer, so the record stays verifiable.

* :func:`configure_bot_logging` — attaches a rotating file handler to the
  trader logger namespace so the "what is the bot doing, and did it work" log is
  written to a file (default ``logs/bot.log``) in addition to the console.

Both are **safe by default**: an empty path disables the record (used in tests),
and every write is best-effort — a logging/CSV failure must never abort or
corrupt a trading cycle.
"""
from __future__ import annotations

import csv
import logging
import os
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

logger = logging.getLogger("collectorcrypt.trader.audit")

# The logger namespace every trader module logs under. Configuring a handler
# here captures engine/executor/manager/audit messages in one file.
BOT_LOGGER_NAME = "collectorcrypt.trader"

# Column order of the transaction ledger CSV. Kept stable and append-only so an
# existing file is never rewritten with a different shape.
LEDGER_COLUMNS: tuple[str, ...] = (
    "timestamp_utc",    # ISO-8601 UTC, human readable
    "timestamp_epoch",  # unix seconds, for sorting/joins
    "cycle_id",         # the trade cycle this event belongs to
    "event",            # buy | offer_placed | offer_filled | offer_bumped |
                        # offer_cancelled | listed | markdown | sold |
                        # offer_accepted
    "kind",             # order kind: buy | offer | list
    "card_name",
    "category",
    "nft_address",      # Solana mint address
    "card_id",          # CollectorCrypt internal card id
    "price_usd",        # amount moved (USDC)
    "market_usd",       # reference market/insured value at the time
    "currency",
    "signature",        # on-chain transaction signature (fee verifiable here)
    "status",           # the resulting order status
    "detail",           # human note
)


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


class TransactionLedger:
    """Append-only CSV record of real money events.

    Thread-safe and best-effort: writes are serialized by a lock and any I/O
    failure is logged and swallowed (the ledger must never crash a cycle). An
    empty ``path`` disables the ledger entirely — :meth:`record` becomes a
    no-op — which keeps tests and dry-run setups from writing files.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = (path or "").strip()
        self._enabled = bool(self._path)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> str:
        return self._path

    def record(self, *, event: str, kind: str = "", card_name: str = "",
               category: str = "", nft_address: str = "", card_id: str = "",
               price_usd: float = 0.0, market_usd: float = 0.0,
               currency: str = "USDC", signature: str = "", status: str = "",
               cycle_id: str = "", detail: str = "",
               now: float | None = None) -> bool:
        """Append one transaction row. Returns ``True`` if it was written.

        Never raises. A disabled ledger (empty path) returns ``False`` without
        touching the filesystem.
        """
        if not self._enabled:
            return False
        ts = time.time() if now is None else float(now)
        row = {
            "timestamp_utc": _iso(ts),
            "timestamp_epoch": f"{ts:.3f}",
            "cycle_id": cycle_id,
            "event": event,
            "kind": kind,
            "card_name": card_name,
            "category": category,
            "nft_address": nft_address,
            "card_id": card_id,
            "price_usd": f"{float(price_usd):.6f}",
            "market_usd": f"{float(market_usd):.6f}",
            "currency": currency,
            "signature": signature,
            "status": status,
            "detail": detail,
        }
        try:
            with self._lock:
                directory = os.path.dirname(self._path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                is_new = (not os.path.exists(self._path)
                          or os.path.getsize(self._path) == 0)
                with open(self._path, "a", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
                    if is_new:
                        writer.writeheader()
                    writer.writerow(row)
            return True
        except OSError as exc:  # best-effort: a record failure must not abort a cycle
            logger.warning("Transaction ledger write failed (%s): %s",
                           self._path, exc)
            return False

    def record_order(self, order: Any, *, event: str,
                     now: float | None = None) -> bool:
        """Append a row from an :class:`~collectorcrypt.trader.orders.Order`.

        Pulls the relevant fields off the order so call sites stay terse. Never
        records a simulated (dry-run) order: the ledger is real trades only.
        """
        if order is None or getattr(order, "simulated", False):
            return False
        kind = getattr(getattr(order, "kind", None), "value", "") or ""
        status = getattr(getattr(order, "status", None), "value", "") or ""
        return self.record(
            event=event,
            kind=kind,
            card_name=getattr(order, "name", "") or "",
            category=getattr(order, "category", "") or "",
            nft_address=getattr(order, "nft", "") or "",
            card_id=getattr(order, "card_id", "") or "",
            price_usd=getattr(order, "price_usd", 0.0) or 0.0,
            market_usd=getattr(order, "market_usd", 0.0) or 0.0,
            currency=getattr(order, "currency", "") or "USDC",
            signature=getattr(order, "signature", "") or "",
            status=status,
            cycle_id=getattr(order, "cycle_id", "") or "",
            detail=getattr(order, "detail", "") or "",
            now=now,
        )


def configure_bot_logging(path: str | None, *, level: int = logging.INFO,
                          max_bytes: int = 1_000_000,
                          backup_count: int = 5) -> bool:
    """Attach a rotating file handler to the trader logger.

    Writes the bot's activity log (cycle outcomes, trades, halts, errors) to
    ``path`` in addition to whatever the application's root logger already does.
    Idempotent: a handler for the same resolved path is only added once, so
    repeated calls (e.g. a manager rebuilt on reload) do not duplicate lines. An
    empty ``path`` disables file logging and returns ``False``. Never raises.
    """
    target = (path or "").strip()
    if not target:
        return False
    bot_logger = logging.getLogger(BOT_LOGGER_NAME)
    try:
        resolved = os.path.abspath(target)
        for handler in bot_logger.handlers:
            existing = getattr(handler, "baseFilename", None)
            if existing and os.path.abspath(existing) == resolved:
                return True  # already configured for this file
        directory = os.path.dirname(resolved)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handler = RotatingFileHandler(
            resolved, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        bot_logger.addHandler(handler)
        if bot_logger.level == logging.NOTSET or bot_logger.level > level:
            bot_logger.setLevel(level)
        return True
    except OSError as exc:  # best-effort: logging setup must never crash startup
        logger.warning("Bot log setup failed (%s): %s", target, exc)
        return False
