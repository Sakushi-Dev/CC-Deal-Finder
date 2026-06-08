# CC-Deal-Finder

> Flask-based marketplace browser, deal scanner and autonomous trading bot for `api.collectorcrypt.com`.

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
- Dark/light theme switcher (persisted in `localStorage`)
- SOL→USD live rate display (10-min cache, updates card values automatically)
- Observe list with live refresh via API and persistent storage (`localStorage`)
- Deal scanner that compares current listings against the CC insured value
  - Category filter (Pokemon, One Piece, MtG, Lorcana, Yu-Gi-Oh!, Sports, …)
  - Configurable price range, shuffle vs. newest-first scan order
- Profile page: paste a Solana wallet address to view its cards plus a quick
  dashboard (count, total / average insured value, per-category breakdown);
  wallet remembered in `localStorage`
- In-memory cache with retry policy for stable API requests
- Unified card layout for marketplace, deals and profile (single source of truth)
- **Autonomous trader (local, dry-run by default)** — scans CC listings, sizes
  purchases against the wallet's available USDC volume, plans direct buys and
  standing offers with a quantity-first strategy, manages the full holdings
  lifecycle (markdown → offer accept), and persists all state to SQLite

## Routes

### Pages

| Route      | Purpose |
|------------|---------|
| `/`        | Marketplace browser with filters, observe list, card/list view |
| `/deals`   | Background scanner — listings vs. CC insured value |
| `/trader`  | Autonomous trader dashboard: controls, P/L, holdings, history, settings |
| `/profile` | Owned cards + value dashboard for a given Solana wallet |

### JSON API

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/card/<nft>` | GET | Single card from the API (observe live refresh) |
| `/api/sol-rate` | GET | Current SOL→USD spot price |
| `/deals/start` | POST | Start scan (price range, order, category) |
| `/deals/status` | GET | Poll scanner snapshot |
| `/deals/{pause,resume,stop}` | POST | Scanner control |
| `/trader/status` | GET | Poll trader state (report, holdings, history, risk, reconciliation) |
| `/trader/wallet` | GET | Live SOL/USDC balances (read-only) |
| `/trader/run` | POST | Run single cycle |
| `/trader/demo` | POST | Simulate cycle with user-supplied USDC volume |
| `/trader/loop/{start,pause,resume,stop}` | POST | Auto-loop control |
| `/trader/blacklist/clear` | POST | Remove NFT from unpopular blacklist |
| `/trader/settings` | GET / POST | Fetch / save strategy overrides |
| `/trader/profiles` | GET | List presets + custom profiles |
| `/trader/profiles/{apply,save,delete}` | POST | Profile management |

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

<details>
<summary><strong>CC-Deal-Finder/</strong></summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;├── app.py                      </code>&nbsp;— Flask entry point<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;├── trade.py                    </code>&nbsp;— trader CLI: --json, --loop SECONDS<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;├── .env.example                </code>&nbsp;— all env vars with descriptions (copy to .env)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;├── trader_settings.example.json</code>&nbsp;— strategy tuning defaults (copy to trader_settings.json)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;├── requirements.txt            </code>&nbsp;— flask, requests, solders, python-dotenv<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;├── start.bat                   </code>&nbsp;— Windows launcher (minimized + opens browser)<br>
<details>
<summary><code>&nbsp;&nbsp;├────── collectorcrypt/</code>&nbsp;— deal-finder core + trader</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── __init__.py   </code>&nbsp;— exports create_app<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── api.py        </code>&nbsp;— CCClient – HTTP + in-memory cache + retry<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── config.py     </code>&nbsp;— URLs, timeouts, retry policy, limits<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── normalize.py  </code>&nbsp;— card parsing, formatting, FX conversion<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── scanner.py    </code>&nbsp;— ScanManager – background worker (deals)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── web.py        </code>&nbsp;— Flask factory + blueprints (views, api)<br>
<details>
<summary><code>&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;└── trader/</code>&nbsp;— autonomous trading bot</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── auth.py       </code>&nbsp;— session model, NullSessionProvider, StaticTokenProvider<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── ccapi.py      </code>&nbsp;— authenticated CC trading client (buy, offer, list, broadcast)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── config.py     </code>&nbsp;— all env vars: secrets from .env, tunables from JSON<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── engine.py     </code>&nbsp;— one decision cycle (source → plan → execute)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── executor.py   </code>&nbsp;— DryRunExecutor + gated LiveExecutor + maintenance<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── holdings.py   </code>&nbsp;— holdings lifecycle: markdown, bump, cancel, accept<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── manager.py    </code>&nbsp;— background worker, loop control, crash recovery<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── orders.py     </code>&nbsp;— Order domain model, lifecycle, audit trail<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── reconcile.py  </code>&nbsp;— Reconciler + StatusSyncer (read-only + authoritative)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── risk.py       </code>&nbsp;— spend caps, open-positions cap, kill-switch<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── settings.py   </code>&nbsp;— UI-editable tunables + strategy profiles<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── siws.py       </code>&nbsp;— Privy SIWS provider (real handshake, token cache)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── store.py      </code>&nbsp;— SQLite persistence (cycles, orders, holdings)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── strategy.py   </code>&nbsp;— buy strategy: quantity-first, escalation (pure)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;└── wallet.py     </code>&nbsp;— Solana RPC balances + local keypair signing<br>
</details>
</details>
<details>
<summary><code>&nbsp;&nbsp;├────── docs/</code>&nbsp;— API documentation</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── index.md         </code>&nbsp;— table of contents<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── api-overview.md  </code>&nbsp;— basics, public /marketplace endpoint, update guide<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── api-endpoints.md </code>&nbsp;— full endpoint registry from the frontend bundle<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── api-auth.md      </code>&nbsp;— Privy SIWS handshake (verified), session providers<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── api-trading.md   </code>&nbsp;— verified trading flows, error & retry policy<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── api-executor.md  </code>&nbsp;— live executor, exit/relisting, holdings passes<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;└── api-risk.md      </code>&nbsp;— risk engine, crash recovery, settings reference<br>
</details>
<details>
<summary><code>&nbsp;&nbsp;├────── static/</code>&nbsp;— CSS + JavaScript</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── css/app.css       </code>&nbsp;— full design (dark + light theme)<br>
<details>
<summary><code>&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;└── js/</code></summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── theme.js          </code>&nbsp;— dark/light switcher + active nav link<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── sol-rate.js       </code>&nbsp;— SOL→USD rate (10-min sessionStorage cache)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── util.js           </code>&nbsp;— formatters (USD/price, escapeHtml)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── lightbox.js       </code>&nbsp;— card preview (flip + parallax)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── obs-badge.js      </code>&nbsp;— observe count badge (cross-tab sync)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── observe-store.js  </code>&nbsp;— persistent observe data (localStorage)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── observe-cards.js  </code>&nbsp;— observe buttons + rebuild + API refresh<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── insured.js        </code>&nbsp;— insured-value footer<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── view-toggle.js    </code>&nbsp;— card/list view (persistent)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── filters.js        </code>&nbsp;— marketplace filter bar<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── marketplace.js    </code>&nbsp;— bindings for /<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── deals.js          </code>&nbsp;— bindings for /deals (poll loop)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── profile.js        </code>&nbsp;— wallet persistence + card wiring<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;└── trader.js         </code>&nbsp;— full trader dashboard (polling, all panels)<br>
</details>
</details>
<details>
<summary><code>&nbsp;&nbsp;├────── templates/</code>&nbsp;— Jinja2 HTML templates</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── base.html     </code>&nbsp;— layout, loads CSS + core JS<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── _card.html    </code>&nbsp;— reusable marketplace card component<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── index.html    </code>&nbsp;— marketplace + observe<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── deals.html    </code>&nbsp;— deals scanner<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── profile.html  </code>&nbsp;— wallet dashboard + owned cards<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;└── trader.html   </code>&nbsp;— trader dashboard (tabs: overview, planned, holdings, history)<br>
</details>
<details>
<summary><code>&nbsp;&nbsp;├────── tests/</code>&nbsp;— pytest test suite</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── conftest.py             </code>&nbsp;— shared fixtures (fake wallet, fake CC client, crypto helpers)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_auth_siws.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_ccapi.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_config_settings.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_engine_live.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_executor_live.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_holdings.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_manager_recovery.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_orders.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_reconcile.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_risk.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_store.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── test_wallet.py</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;└── test_web_trader.py</code><br>
</details>
<details>
<summary><code>&nbsp;&nbsp;├────── TODO/</code>&nbsp;— planning & checklists</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── go-live-checklist.md      </code>&nbsp;— actionable punch list before live trading<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── live-readiness-plan.md    </code>&nbsp;— full verification route + per-shape evidence<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;├── holdings-lifecycle-plan.md</code><br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;└── code-review-findings.md</code><br>
</details>
<details>
<summary><code>&nbsp;&nbsp;└────── tools/</code>&nbsp;— development utilities</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── _tree.py              </code>&nbsp;— prints current project file tree<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── discover_endpoints.py </code>&nbsp;— extract API paths from the frontend bundle<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── probe_live.py         </code>&nbsp;— read-only live API probe (no orders placed)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── live_offer_check.py   </code>&nbsp;— reversible escrow-offer verification<br>
<details>
<summary><code>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;└── captures/</code>&nbsp;— DevTools curl captures (gitignored)</summary>
<code>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── requests/   </code>&nbsp;— curl request captures (.bash)<br>
<code>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;└── responses/  </code>&nbsp;— response body captures (.bash)<br>
</details>
</details>
</details>

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

- **CC only** — listings from other marketplaces (e.g. Magic Eden / `ME`) are ignored
  (`TRADER_ALLOWED_MARKETPLACES`, default `"CC"`).
- **Available volume** — the wallet's on-chain USDC balance (minus `TRADER_RESERVE_USDC`
  and a SOL gas reserve) determines how much the bot may still spend.
- **Direct buys vs. offers** — volume is split via `TRADER_DIRECT_BUY_PCT` /
  `TRADER_OFFER_PCT`. Offers are placed `TRADER_OFFER_DISCOUNT_PCT` below the ask.
  If both sum to more than 100 they are scaled down proportionally.
- **Quantity over quality** — qualifying listings are bought cheapest-first.
- **Escalation protocol** — above `TRADER_ESCALATION_VOLUME_USD` (default 1 000 USDC)
  the per-card cap is raised to `TRADER_ESCALATION_MAX_CARD_USD`.
- **Holdings lifecycle** — after buying, cards are actively managed:
  markdown toward cost-basis floor, offer-bumping to re-notify owners, and
  automatic offer-accept once the card has rested at the floor long enough.

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

For a **dry-run** you only need `TRADER_WALLET_ADDRESS` (read-only) and a Solana
RPC URL. Live execution is gated behind **three** independent conditions, all of
which must hold before a single real transaction is signed:

1. `TRADER_LIVE=true` (master switch, env-only — never settable from the UI),
2. a usable `TRADER_WALLET_SECRET` (the local signing keypair), **and**
3. a configured auth provider (`TRADER_AUTH_PROVIDER=privy` or `static`).

If any one is missing the bot stays in dry-run — **plans only, spends nothing**.
Writes are **never auto-retried** (double-spend guard). See
**[Live readiness](#live-readiness)** before enabling live mode.

### Dashboard (`/trader`)

The web dashboard at `/trader` polls `/trader/status` every 4 seconds and renders:

- **KPI grid** — wallet address, USDC, SOL, available volume, direct/offer budgets,
  per-card cap (escalation marker), unrealized resale profit
- **Status / live bar** — arm status, auth provider, active orders, reconciliation state
- **Risk bar** — daily spend vs. cap, open positions, consecutive failures, halt reason
- **Recovery bar** — auto-resume notice if loop was restarted after a crash
- **Overview tab** — P/L snapshot with mode, budgets, scan stats
- **Planned orders tab** — direct buys, offers, closest deals (near-miss scan)
- **Holdings tab** — inventory table, maintenance bar, bumped offers, unpopular
  blacklist (per-row clear button)
- **History tab** — past cycles + totals aggregator
- **Executed orders** — last cycle's buys/offers with on-chain status
- **Exit section** (live only) — relisted cards + status-sync summary
- **Settings drawer** — strategy presets (*Direct flip*, *Balanced 50/50*,
  *Patient offers*, custom), all tuning knobs with info buttons

### Audit trail (bot log + transaction ledger)

For provable record-keeping (e.g. tax or regulatory evidence) the bot writes two
operational records. Both are **off by default in tests**, contain real wallet /
trade data, and are **git-ignored** — never commit them.

| Record | Default path | Env var | Contents |
| --- | --- | --- | --- |
| **Bot activity log** | `logs/bot.log` | `TRADER_LOG_PATH` | Per-cycle summary (mode, scanned, planned buys/offers, fills, order states) + each money action with success/failure. Rotating, 1 MB × 5 files. |
| **Transaction ledger** | `records/transactions.csv` | `TRADER_LEDGER_PATH` | One append-only CSV row per **real** transaction. |

The ledger columns are:

```
timestamp_utc, timestamp_epoch, cycle_id, event, kind, card_name, category,
nft_address, card_id, price_usd, market_usd, currency, signature, status, detail
```

Recorded `event`s: `buy`, `offer_placed`, `offer_filled`, `listed`,
`offer_bumped`, `offer_cancelled`, `markdown`, `offer_accepted`, `sold`.

Notes:

- Only **real (non-simulated)** trades are recorded — dry-run and demo cycles
  write nothing.
- The on-chain `signature` lets anyone re-derive the exact network (gas) fee from
  a block explorer, so fees stay verifiable without being stored.
- Set either env var to an **empty value** to disable that record; an absent
  variable falls back to the default path.

## Live readiness

> **Status: Beta / dry-run-ready / live-integration in verification.**
> Not yet cleared for unattended live operation.

SIWS authentication and a **reversible on-chain escrow offer**
(place → sign → broadcast → settle → cancel → refund) have been executed
end-to-end on a funded test wallet. Before the loop may run unattended with real
money, the following **mandatory** steps remain:

1. Set all risk limits to non-zero values (currently all default to `0` / disabled):
   `TRADER_MAX_SPEND_PER_CYCLE_USD`, `TRADER_MAX_SPEND_PER_DAY_USD`,
   `TRADER_MAX_OPEN_POSITIONS`, `TRADER_MAX_CONSECUTIVE_FAILURES`,
   `TRADER_RESERVE_USDC`, `TRADER_GAS_RESERVE_SOL`
2. Replace the public RPC with a dedicated endpoint (Helius / QuickNode / Triton)
3. Execute one supervised tiny direct buy (sign → broadcast → settle)
4. Execute one create-listing + cancel-listing cycle
5. Operational hardening: monitoring, alerting, runbook, auto-resume review

Full tracking in:

- **[TODO/live-readiness-plan.md](TODO/live-readiness-plan.md)** — verification
  route + per-shape evidence (single source of truth)
- **[TODO/go-live-checklist.md](TODO/go-live-checklist.md)** — concise actionable
  punch list

> ⚠️ **Do not run with `TRADER_LIVE=true` until the go-live checklist is
> complete.**

## Architecture

- **Separation of data / transport / UI** — `normalize.py` is purely functional
  and knows neither Flask nor HTTP. `api.py` does transport only. `scanner.py`
  orchestrates the worker and uses both.

- **Single source of truth for the card layout** — marketplace, deals and profile
  use the same `.card` structure, CSS and observe/lightbox modules. Deals renders
  client-side (live poll), marketplace and profile server-side.

- **Card and list view** — a single CSS class (`grid.view-list`) used identically
  on both pages.

- **Trader isolation** — the entire trader subsystem lives under
  `collectorcrypt/trader/` and is never imported by the deal-finder pages.
  The web layer calls only `TraderManager` methods; all business logic is internal.

- **Pure strategy** — `strategy.py` and `holdings.py` contain only decision logic
  (no I/O, no side effects). Both dry-run and live pipelines share identical inputs.

## Development

Further API details are in [`docs/`](docs/index.md) — see the index for the full
documentation split (overview, endpoint registry, auth, trading flows, executor,
risk engine).

```powershell
# print current file tree
python tools/_tree.py

# discover new/changed API endpoints from the frontend bundle
python tools/discover_endpoints.py

# read-only live probe (no orders placed, no funds spent)
python tools/probe_live.py

# run tests
pytest
```

## License

This project is licensed under the [MIT License](LICENSE).




