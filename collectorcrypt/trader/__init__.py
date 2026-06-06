"""Autonomous trader package.

Public surface:

* :func:`load_config` / :class:`TraderConfig` — settings from env / .env.
* :class:`Wallet` — read SOL/USDC, derive address, (live) sign.
* :class:`TradeEngine` — run a decision cycle (dry-run by default).
"""
from __future__ import annotations

from .auth import (AuthSession, NullSessionProvider, SessionProvider,
                   StaticTokenProvider)
from .ccapi import (CCApiError, CCAuthError, CCClientError, CCNetworkError,
                    CCRateLimitError, CCServerError, CCTradingClient, redact)
from .config import TraderConfig, load_config
from .engine import TradeEngine
from .manager import TraderManager
from .orders import (Order, OrderError, OrderKind, OrderStatus,
                     make_client_order_id, plan_to_orders)
from .reconcile import (Reconciler, ReconciliationReport, StatusSyncer,
                        StatusSyncReport)
from .risk import RiskDecision, RiskEngine
from .siws import (PrivySiwsProvider, check_live_ready,
                   make_session_provider)
from .store import OrderStore
from .wallet import Wallet, WalletError

__all__ = [
    "TraderConfig", "load_config", "TradeEngine", "TraderManager",
    "Wallet", "WalletError",
    "Order", "OrderError", "OrderKind", "OrderStatus",
    "make_client_order_id", "plan_to_orders",
    "OrderStore", "Reconciler", "ReconciliationReport", "StatusSyncer",
    "StatusSyncReport", "RiskEngine", "RiskDecision",
    "AuthSession", "SessionProvider", "NullSessionProvider",
    "StaticTokenProvider",
    "CCTradingClient", "CCApiError", "CCAuthError", "CCRateLimitError",
    "CCClientError", "CCServerError", "CCNetworkError", "redact",
    "PrivySiwsProvider", "make_session_provider", "check_live_ready",
]
