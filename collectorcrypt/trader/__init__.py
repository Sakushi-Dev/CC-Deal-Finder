"""Autonomous trader package.

Public surface:

* :func:`load_config` / :class:`TraderConfig` — settings from env / .env.
* :class:`Wallet` — read SOL/USDC, derive address, (live) sign.
* :class:`TradeEngine` — run a decision cycle (dry-run by default).
"""
from __future__ import annotations

from .config import TraderConfig, load_config
from .engine import TradeEngine
from .manager import TraderManager
from .wallet import Wallet, WalletError

__all__ = [
    "TraderConfig", "load_config", "TradeEngine", "TraderManager",
    "Wallet", "WalletError",
]
