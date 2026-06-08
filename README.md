# CC-Deal-Finder

> Flask-based marketplace browser and deal scanner for the public `api.collectorcrypt.com`.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.0%2B-black.svg)](https://flask.palletsprojects.com/)
[![Status](https://img.shields.io/badge/status-active-success.svg)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## ⚠️ Disclaimer — experimental software, no warranty

**This application is purely experimental and is provided "as is", without
warranty of any kind.** It can plan and (when explicitly enabled) execute
financial transactions involving real cryptocurrency and NFTs. Markets are
volatile and the trading logic may contain bugs.

**The author accepts no liability whatsoever for any loss of money, tokens, or
assets incurred through the use of this application.** You use it entirely at
your own risk. Nothing here is financial advice. Always review the code, test
in dry-run mode, and never trade with funds you cannot afford to lose.

---

## Table of contents

- [Features](#features)
- [Routes](#routes)
- [Installation](#installation)
- [Run](#run)
- [Project structure](#project-structure)
- [Autonomous trader](#autonomous-trader)
- [Live readiness](#live-readiness)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

---

## Features

- Marketplace browser with filters, card/list view and lightbox preview
- Observe list with live refresh via API and persistent storage (`localStorage`)
- Deal scanner that compares current listings against the CC insured value
  - Category filter (Pokemon, One Piece, MtG, Lorcana, Yu-Gi-Oh!, Sports, …)
  - Configurable delay between page fetches to avoid middleware rate-limit blocks
  - “Newest first” scan order is the default
- Profile page: paste a Solana wallet address to view its cards plus a quick
  dashboard (count, total / average insured value, per-category breakdown);
  the wallet is remembered in `localStorage`
- In-memory cache with retry policy for stable API requests
- Unified card layout for marketplace, deals and profile (single source of truth)
- **Autonomous trader (local, dry-run by default)** — scans CC listings, sizes
  purchases against the wallet's available USDC volume and plans buys with a
  quantity-first strategy plus an escalation protocol; see
  [Autonomous trader](#autonomous-trader)

## Routes

| Route               | Purpose                                                                |
| ------------------- | ---------------------------------------------------------------------- |
| `/`                 | Marketplace browser with filters, observe list, card/list view        |
| `/deals`            | Background scanner comparing listings vs. CC insured value             || `/trader`           | Autonomous trader dashboard: controls, P/L, history, live settings      || `/profile`          | Owned cards + value dashboard for a given Solana wallet                |
| `/api/card/<nft>`   | Single card fresh from the API (used for observe live refresh)         |

## Installation

**Requirements:** Python 3.10+ and `pip`.

```powershell
# Clone the repository
git clone https://github.com/Sakushi-Dev/CC-Deal-Finder.git
cd CC-Deal-Finder

# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

> **Linux/macOS:** use `source .venv/bin/activate` instead of the PowerShell activation.

## Run

```powershell
python app.py
```

Then open <http://127.0.0.1:5000> in the browser.

Alternatively, on Windows double-click [`start.bat`](start.bat) — starts the app minimized and opens the browser automatically.

## Project structure

```text
.
├── app.py                       # Entry point – builds the app via the factory
├── requirements.txt             # Flask, requests, solders, python-dotenv
├── start.bat                    # Windows launcher
├── trade.py                     # Autonomous trader CLI (dry-run by default)
├── .env.example                 # Connection & security vars (copy to .env)
├── trader_settings.example.json # Strategy tuning defaults (copy to trader_settings.json)
├── collectorcrypt/
│   ├── __init__.py              # exports create_app
│   ├── config.py                # URLs, timeouts, retry policy, limits
│   ├── normalize.py             # card parsing, formatting, FX conversion
│   ├── api.py                   # CCClient – HTTP + in-memory cache + retry
│   ├── scanner.py               # ScanManager – background worker (deals)
│   ├── web.py                   # Flask factory + blueprints (views, api)
│   └── trader/                  # Autonomous trader (local, dry-run by default)
│       ├── config.py            # config: secrets from .env, tunables from JSON
│       ├── settings.py          # UI-editable tunables + strategy profiles
│       ├── wallet.py            # Solana RPC balances + (live) keypair signing
│       ├── auth.py / siws.py    # SIWS (sign-in-with-Solana) session providers
│       ├── ccapi.py             # authenticated CC trading client (writes)
│       ├── strategy.py          # CC-only, quantity-first, escalation (pure)
│       ├── orders.py / store.py # order/holding models + SQLite persistence
│       ├── risk.py              # spend caps + kill-switch posture
│       ├── manager.py / reconcile.py # loop control + crash recovery
│       ├── engine.py            # one decision cycle (source → plan → execute)
│       └── executor.py          # DryRunExecutor + gated LiveExecutor
├── templates/
│   ├── base.html                # layout, loads CSS + core JS
│   ├── _card.html               # reusable marketplace card
│   ├── index.html               # marketplace + observe
│   ├── deals.html              # deals scanner (cards + list)
│   └── profile.html            # wallet dashboard + owned cards
├── static/
│   ├── css/app.css              # full design
│   └── js/
│       ├── util.js              # formatters (USD/price, escapeHtml)
│       ├── lightbox.js          # card preview (flip + parallax)
│       ├── observe-store.js     # persistent observe data (localStorage)
│       ├── observe-cards.js     # observe buttons + rebuild + API refresh
│       ├── insured.js           # insured-value footer
│       ├── view-toggle.js       # card/list view (persistent)
│       ├── filters.js           # marketplace filter bar
│       ├── marketplace.js       # bindings for /
│       ├── deals.js             # bindings for /deals (incl. poll loop)
│       └── profile.js           # wallet persistence + card wiring
├── tools/
│   └── discover_endpoints.py    # reverse-engineers the CC API
└── docs/
    └── api.md                   # API documentation
```

## Autonomous trader

A local, opt-in trading bot that accumulates cards by buying under-priced
**CollectorCrypt** listings. It lives in `collectorcrypt/trader/` and runs from
the `trade.py` CLI. **Dry-run is the default — it never spends anything until
you explicitly enable live mode.**

### Why not Phantom directly?

Phantom is a UI-approval wallet: every transaction requires a human click, so it
**cannot be driven autonomously**. For unattended trading the bot signs with a
local keypair instead — the same secret Phantom uses, which you can export via
*Phantom → Settings → Security & Privacy → Export Private Key*. The bot then *is*
that wallet; Phantom is only where it was created and funded.

> Use a **dedicated, separately funded** wallet. Treat the private key like cash.
> It is read from a gitignored `.env` and never committed.

### How it decides

- **CC only** — listings from other marketplaces (e.g. Magic Eden / `ME`) are ignored.
- **Available volume** — the wallet's on-chain USDC balance (minus a reserve)
  determines how much the bot may still spend.
- **Direct buys vs. offers** — the available volume is split between instant
  purchases and standing buy orders (offers/bids). The split is configurable via
  `TRADER_DIRECT_BUY_PCT` / `TRADER_OFFER_PCT` (percent of volume; if they sum to
  more than 100 they are scaled down). Offers are placed
  `TRADER_OFFER_DISCOUNT_PCT` **below** the listed ask price.
- **Quantity over quality** — qualifying listings are bought cheapest-first to
  accumulate as many cards as possible.
- **Escalation protocol** — once the available volume reaches a threshold
  (`TRADER_ESCALATION_VOLUME_USD`, default 1000 USDC), the per-card price cap is
  raised so expensive cards become eligible too.

### Setup & run

```powershell
# 1. configure connection & security (copy template, then edit .env)
copy .env.example .env

# 1b. (optional) start from the strategy tuning defaults
copy trader_settings.example.json trader_settings.json

# 2. dry-run: one cycle, human-readable report (spends nothing)
python trade.py

# JSON output / continuous loop
python trade.py --json
python trade.py --loop 300
```

For a **dry-run** you only need `TRADER_WALLET_ADDRESS` (read-only) and an RPC
URL. Live execution is gated behind **three** independent conditions, all of
which must hold before a single real transaction is signed:

1. `TRADER_LIVE=true` (the master switch, env-only — never settable from the UI),
2. a usable `TRADER_WALLET_SECRET` (the local signing keypair), **and**
3. a configured auth provider so the bot can authenticate to CollectorCrypt.

If any one is missing the bot stays in dry-run and **plans only — it spends
nothing**. The `LiveExecutor` itself is implemented and its money-moving path
(make-offer → local sign → broadcast → on-chain settle → cancel/refund) has been
verified end-to-end on a funded test wallet. Writes are **never auto-retried**
(double-spend guard). See **[Live readiness](#live-readiness)** before enabling
live mode — several mandatory steps (risk caps, dedicated RPC, supervised
buy test) are still required.

### Dashboard (`/trader`)

The bot also has a web dashboard inside the app at `/trader`:

- **Controls** — run a single cycle, or start a repeating loop (pause / resume / stop).
- **P/L** — wallet balances, available volume, direct/offer budget split,
  per-card cap (with escalation marker) and the unrealized “book” profit
  (insured value − planned cost).
- **Planned orders** — the direct buys and offers the bot would place this cycle.
- **History** — every completed cycle, persisted locally to the git-ignored
  `trade_history.jsonl`.
- **Settings** — edit the strategy tuning knobs at runtime (saved to the
  git-ignored `trader_settings.json`; defaults ship in
  `trader_settings.example.json`), grouped into labelled sections with a
  per-setting info button. **Strategy profiles** let you switch the whole
  strategy in one click — three built-in presets (*Direct flip*, *Balanced
  50/50*, *Patient offers*) plus saveable custom profiles. The private key, the
  live switch, the auth credentials and auto-resume stay in `.env` and **cannot**
  be changed from the UI.

## Live readiness

> **Status: dry-run-ready; live integration verified for the reversible escrow
> path, not yet cleared for unattended live operation.**

The hardest part is proven: SIWS authentication and a **reversible on-chain
escrow offer** (place → sign → broadcast → settle → cancel → refund) have been
executed end-to-end on a funded test wallet. Before the loop may run unattended
with real money, several **mandatory** steps remain — risk caps, a dedicated
RPC, and one supervised tiny buy. These are tracked in:

- **[docs/live-readiness-plan.md](docs/live-readiness-plan.md)** — the full
  verification route and per-shape evidence (single source of truth).
- **[docs/go-live-checklist.md](docs/go-live-checklist.md)** — the concise,
  actionable list of what is still open before live.

> ⚠️ **Do not run with `TRADER_LIVE=true` until the go-live checklist is
> complete.** In particular, all risk limits currently default to `0`
> (disabled); they must be set to non-zero values first.

## Architecture

- **Separation of data / transport / UI**
  `normalize.py` is purely functional and knows neither Flask nor HTTP.
  `api.py` does transport only. `scanner.py` orchestrates the worker and uses both.

- **Single source of truth for the card layout**
  Marketplace, deals and profile use the same `.card` structure, the same CSS
  and the same observe/lightbox modules. Deals renders cards client-side (live),
  marketplace and profile server-side — functionally identical.

- **Card and list view**
  Implemented via a single CSS class (`grid.view-list`) and used identically on both pages.

## Development

Further API details are in [`docs/`](docs/index.md) — see the index for the full documentation split.

To discover previously unknown endpoints:

```powershell
python tools/discover_endpoints.py
```

## License

This project is licensed under the [MIT License](LICENSE).
