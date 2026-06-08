"""Audit trail tests — transaction ledger (CSV) + bot activity log.

The ledger is the provable, append-only record of every real money event; the
log captures what the bot did. Both must be best-effort (never crash a cycle),
must never record a simulated (dry-run) order, and must stay disabled when no
path is configured (so tests and dry-run setups write no files).
"""
from __future__ import annotations

import csv
import logging

import pytest

from collectorcrypt.trader.audit import (BOT_LOGGER_NAME, LEDGER_COLUMNS,
                                          TransactionLedger,
                                          configure_bot_logging)
from collectorcrypt.trader.executor import LiveExecutor
from collectorcrypt.trader.orders import OrderKind, OrderStatus

from .conftest import FakeClient, FakeWallet, make_buy, make_config, make_offer


def _read_rows(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _make_live(client=None, *, ledger=None, volume=1000.0):
    return LiveExecutor(
        FakeWallet(can_sign=True), "https://rpc.test.invalid",
        client=client or FakeClient(), available_volume=volume,
        cfg=make_config(live=True), ledger=ledger)


# --------------------------------------------------------------------------- #
# TransactionLedger — disabled by default (empty path)
# --------------------------------------------------------------------------- #
def test_empty_path_disables_ledger(tmp_path):
    ledger = TransactionLedger("")
    assert ledger.enabled is False
    assert ledger.record(event="buy", price_usd=10) is False


def test_disabled_ledger_writes_no_file(tmp_path):
    ledger = TransactionLedger("")
    ledger.record(event="buy", price_usd=10)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# TransactionLedger — writing rows
# --------------------------------------------------------------------------- #
def test_record_creates_file_with_header(tmp_path):
    path = tmp_path / "records" / "transactions.csv"
    ledger = TransactionLedger(str(path))
    assert ledger.record(event="buy", card_name="Charizard", price_usd=12.5,
                         currency="USDC", nft_address="NFT1") is True
    assert path.exists()
    rows = _read_rows(path)
    assert len(rows) == 1
    assert list(rows[0].keys()) == list(LEDGER_COLUMNS)
    assert rows[0]["event"] == "buy"
    assert rows[0]["card_name"] == "Charizard"
    assert rows[0]["price_usd"] == "12.500000"
    assert rows[0]["nft_address"] == "NFT1"


def test_record_creates_separate_directory(tmp_path):
    path = tmp_path / "ledger_dir" / "txns.csv"
    TransactionLedger(str(path)).record(event="buy", price_usd=1)
    assert path.parent.is_dir()


def test_multiple_records_append_one_header(tmp_path):
    path = tmp_path / "transactions.csv"
    ledger = TransactionLedger(str(path))
    ledger.record(event="buy", price_usd=1)
    ledger.record(event="sold", price_usd=2)
    rows = _read_rows(path)
    assert [r["event"] for r in rows] == ["buy", "sold"]
    # Header appears exactly once (csv.DictReader consumed it; the data file has
    # only two data lines + one header line).
    with open(path, encoding="utf-8") as fh:
        assert sum(1 for _ in fh) == 3


def test_record_timestamp_is_deterministic_when_injected(tmp_path):
    path = tmp_path / "transactions.csv"
    TransactionLedger(str(path)).record(event="buy", price_usd=1, now=1_700_000_000)
    row = _read_rows(path)[0]
    assert row["timestamp_epoch"] == "1700000000.000"
    assert row["timestamp_utc"].startswith("2023-11-14T")


# --------------------------------------------------------------------------- #
# record_order — maps an Order, skips simulated
# --------------------------------------------------------------------------- #
def test_record_order_maps_fields(tmp_path):
    path = tmp_path / "transactions.csv"
    ledger = TransactionLedger(str(path))
    order = make_buy(nft="NFTX", price_usd=9.0, market_usd=20.0, card_id="CID1")
    order.signature = "SIG123"
    order.name = "Pikachu"
    assert ledger.record_order(order, event="buy") is True
    row = _read_rows(path)[0]
    assert row["nft_address"] == "NFTX"
    assert row["card_id"] == "CID1"
    assert row["signature"] == "SIG123"
    assert row["card_name"] == "Pikachu"
    assert row["kind"] == "buy"


def test_record_order_skips_simulated(tmp_path):
    path = tmp_path / "transactions.csv"
    ledger = TransactionLedger(str(path))
    order = make_buy(simulated=True)
    assert ledger.record_order(order, event="buy") is False
    assert not path.exists()


def test_record_order_none_is_safe(tmp_path):
    ledger = TransactionLedger(str(tmp_path / "transactions.csv"))
    assert ledger.record_order(None, event="buy") is False


# --------------------------------------------------------------------------- #
# LiveExecutor integration — money events reach the ledger
# --------------------------------------------------------------------------- #
def test_confirmed_buy_records_ledger_row(tmp_path):
    path = tmp_path / "transactions.csv"
    ledger = TransactionLedger(str(path))
    executor = _make_live(ledger=ledger)
    executor.execute([make_buy(price_usd=10, market_usd=20, resell_usd=0)])
    rows = _read_rows(path)
    assert [r["event"] for r in rows] == ["buy"]
    assert rows[0]["status"] == "confirmed"


def test_open_offer_records_offer_placed(tmp_path):
    path = tmp_path / "transactions.csv"
    ledger = TransactionLedger(str(path))
    executor = _make_live(ledger=ledger)
    executor.execute([make_offer(price_usd=6, market_usd=20)])
    rows = _read_rows(path)
    assert rows[0]["event"] == "offer_placed"


def test_dry_run_buy_records_nothing(tmp_path):
    # The dry-run executor never touches the ledger (simulated orders only).
    from collectorcrypt.trader.executor import DryRunExecutor

    path = tmp_path / "transactions.csv"
    DryRunExecutor().execute([make_buy(simulated=True, price_usd=10,
                                       market_usd=20)])
    assert not path.exists()


def test_live_executor_without_ledger_does_not_crash():
    # No ledger wired in: money events still settle, just unrecorded.
    executor = _make_live(ledger=None)
    [buy] = [o for o in executor.execute([make_buy(price_usd=10, market_usd=20,
                                                   resell_usd=0)])
             if o.kind is OrderKind.BUY]
    assert buy.status is OrderStatus.CONFIRMED


# --------------------------------------------------------------------------- #
# configure_bot_logging
# --------------------------------------------------------------------------- #
def test_empty_log_path_is_disabled():
    assert configure_bot_logging("") is False


def test_configure_bot_logging_writes_to_file(tmp_path):
    path = tmp_path / "logs" / "bot.log"
    bot_logger = logging.getLogger(BOT_LOGGER_NAME)
    added = configure_bot_logging(str(path))
    try:
        assert added is True
        assert path.exists()
        logging.getLogger("collectorcrypt.trader.test").info("hello-audit")
        for handler in bot_logger.handlers:
            handler.flush()
        assert "hello-audit" in path.read_text(encoding="utf-8")
    finally:
        _remove_file_handlers(bot_logger, path)


def test_configure_bot_logging_is_idempotent(tmp_path):
    path = tmp_path / "bot.log"
    bot_logger = logging.getLogger(BOT_LOGGER_NAME)
    before = len(bot_logger.handlers)
    try:
        configure_bot_logging(str(path))
        configure_bot_logging(str(path))
        added = sum(
            1 for h in bot_logger.handlers
            if getattr(h, "baseFilename", "").endswith("bot.log"))
        assert added == 1
    finally:
        _remove_file_handlers(bot_logger, path)
        assert len(bot_logger.handlers) == before


def _remove_file_handlers(bot_logger, path) -> None:
    """Detach handlers this test added so the global logger stays clean."""
    for handler in list(bot_logger.handlers):
        base = getattr(handler, "baseFilename", "")
        if base and base.endswith(path.name):
            handler.close()
            bot_logger.removeHandler(handler)
