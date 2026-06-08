# CollectorCrypt — Documentation Index

> Reverse-engineered API notes · Trading client reference · Project architecture
>
> Base: `https://api.collectorcrypt.com` · Bundle snapshot: 2026-06-01

---

## Sections

| File | Contents |
|------|----------|
| [api-overview.md](api-overview.md) | API basics, confirmed public endpoints, how to regenerate the endpoint list |
| [api-endpoints.md](api-endpoints.md) | Full endpoint registry extracted from the frontend bundle |
| [api-auth.md](api-auth.md) | Authentication: Privy SIWS handshake (verified), session providers |
| [api-trading.md](api-trading.md) | Verified trading flows: buy, offers, listing, broadcast, error & retry policy |
| [api-executor.md](api-executor.md) | Live executor, exit/relisting, holdings maintenance passes |
| [api-risk.md](api-risk.md) | Risk engine, crash recovery, open/unverified items |

---

## Source files

### Deal finder (public)

| File | Purpose |
|------|---------|
| [collectorcrypt/api.py](../collectorcrypt/api.py) | HTTP client (marketplace + Coinbase), cache, retry |
| [collectorcrypt/scanner.py](../collectorcrypt/scanner.py) | Background deal scanner, worker thread, match logic |
| [collectorcrypt/normalize.py](../collectorcrypt/normalize.py) | Card normalization → unified UI schema |
| [collectorcrypt/config.py](../collectorcrypt/config.py) | App constants (URLs, limits, retry policy) |
| [collectorcrypt/web.py](../collectorcrypt/web.py) | Flask app factory, HTML + JSON API routes |
| [app.py](../app.py) | Flask entry point |

### Trader (authenticated)

| File | Purpose |
|------|---------|
| [collectorcrypt/trader/ccapi.py](../collectorcrypt/trader/ccapi.py) | Authenticated transport: buy, offer, list, broadcast |
| [collectorcrypt/trader/auth.py](../collectorcrypt/trader/auth.py) | Session model, `NullSessionProvider`, `StaticTokenProvider` |
| [collectorcrypt/trader/siws.py](../collectorcrypt/trader/siws.py) | Privy SIWS provider (real handshake, token cache) |
| [collectorcrypt/trader/engine.py](../collectorcrypt/trader/engine.py) | Trade cycle orchestration (`TradeEngine`) |
| [collectorcrypt/trader/executor.py](../collectorcrypt/trader/executor.py) | `DryRunExecutor` / `LiveExecutor`, maintenance actions |
| [collectorcrypt/trader/strategy.py](../collectorcrypt/trader/strategy.py) | Buy strategy, candidate ranking, offer planning |
| [collectorcrypt/trader/risk.py](../collectorcrypt/trader/risk.py) | Risk gate (kill switch, position caps, spend limits) |
| [collectorcrypt/trader/manager.py](../collectorcrypt/trader/manager.py) | Background worker, UI snapshots, crash recovery |
| [collectorcrypt/trader/reconcile.py](../collectorcrypt/trader/reconcile.py) | `Reconciler`, `StatusSyncer` |
| [collectorcrypt/trader/holdings.py](../collectorcrypt/trader/holdings.py) | Holdings lifecycle: markdown, bump, cancel, accept |
| [collectorcrypt/trader/orders.py](../collectorcrypt/trader/orders.py) | `Order` domain model, lifecycle, audit trail |
| [collectorcrypt/trader/store.py](../collectorcrypt/trader/store.py) | SQLite persistence (cycles, orders, holdings) |
| [collectorcrypt/trader/wallet.py](../collectorcrypt/trader/wallet.py) | Solana wallet: balance, sign message/transaction |
| [collectorcrypt/trader/settings.py](../collectorcrypt/trader/settings.py) | UI-editable settings, presets, user profiles |
| [trade.py](../trade.py) | CLI entry point (single cycle / loop) |

### Tools

| File | Purpose |
|------|---------|
| [tools/discover_endpoints.py](../tools/discover_endpoints.py) | Extract API paths from the frontend bundle |
| [tools/probe_live.py](../tools/probe_live.py) | Read-only live API probe (no orders placed) |
| [tools/live_offer_check.py](../tools/live_offer_check.py) | Reversible escrow-offer verification (sign + broadcast) |
