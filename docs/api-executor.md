# CollectorCrypt API — Executor, Exit & Holdings

← [Index](index.md)

---

## Live executor

Source: [collectorcrypt/trader/executor.py](../collectorcrypt/trader/executor.py)

Two executor implementations share one interface:

| Class | Description |
|-------|-------------|
| `DryRunExecutor` | Resolves orders in-memory. No funds spent. Simulates all transitions for the cycle report. |
| `LiveExecutor` | Full on-chain flow: preflight → sign → broadcast → maintenance actions. |

Live mode is armed **only** when `TRADER_LIVE=true` **and** a signing wallet
**and** a non-`none` auth provider are all present.

### Per-order state machine

```
PLANNED ─preflight─▶ SUBMITTED ─sign─▶ SIGNED ─broadcast─▶ PENDING ─▶ CONFIRMED  (buy)
                                                                   └─▶ OPEN       (offer rests)
   └───────────────────────── any failure ─────────────────────────▶ FAILED
```

State persisted after every transition in [collectorcrypt/trader/store.py](../collectorcrypt/trader/store.py).

### Preflight checks (before every send)

1. **Duplicate guard** — if the store already holds the same `client_order_id`
   past `PLANNED`, the order is skipped.
2. **Budget guard** — order cost must fit the remaining per-cycle budget envelope.
3. **Price/market sanity** — refuses to trade with no market reference, or at/above
   market value.

A failed check fails *that* order and the batch continues.

### Sign + broadcast

Unsigned transaction returned by prepare is signed by `Wallet.sign_transaction()`
(base64-decode → `solders` `VersionedTransaction` → re-encode base64) and
broadcast. A broadcast accepted but not yet on-chain stays `PENDING` for the
reconciler.

Source: [collectorcrypt/trader/wallet.py](../collectorcrypt/trader/wallet.py)

### Relisting

A confirmed buy with a positive resale price creates a linked `LIST` order as a
`PLANNED` relist candidate (see exit/relisting below).

### Open / unverified (executor)

- Exact response keys for the unsigned transaction.
- Transaction wire encoding (base64 assumed) and version (v0 assumed, legacy fallback).
- Whether the wallet is the sole required signer at prepare stage.
- Whether an accepted offer reports settlement on `broadcast` or only via a later sync.

---

## Exit / relisting + status sync

Source: [collectorcrypt/trader/reconcile.py](../collectorcrypt/trader/reconcile.py),
[collectorcrypt/trader/executor.py](../collectorcrypt/trader/executor.py)

Run at the end of every **live** cycle (never in dry-run/demo), after new buys/offers.

### 1. Status sync (`StatusSyncer`)

Read-only reconciliation of in-flight orders (`SUBMITTED`/`SIGNED`/`PENDING`/`OPEN`)
against CollectorCrypt's real status:

- reported confirmed → `CONFIRMED` (spawns linked relist candidate, idempotently)
- reported accepted/filled → `CONFIRMED`
- reported cancelled/withdrawn/expired → `CANCELLED`

An order with no external id, unreadable status, or ambiguous response is left
untouched. A read error **never** transitions an order.

Cycle report gains `status_sync` (`StatusSyncReport`):
`checked` / `confirmed` / `cancelled` / `relisted_spawned` / `unresolved` / `errors` / `transitions`.

### 2. Exit / relisting (`LiveExecutor.relist`)

Loads persisted relist candidates (`store.relist_candidates()`) and drives each
onto the market via `marketplace/list` → sign → broadcast, using the same
per-order state machine.

- Sell-side preflight: positive resale price + duplicate-listing guard (no
  budget check — listing does not spend USDC).
- Relist price: `market × (1 − TRADER_RESELL_DISCOUNT_PCT/100)`.

Running the sync *before* the exit pass means a buy confirmed this cycle can
still be listed in the same cycle.

Cycle report gains `relisted` (per-listing summaries).

### Open / unverified (exit + sync)

- Exact status-probe endpoint and payload per order kind.
- Authoritative status vocabulary (matcher accepts broad confirmed/cancelled synonyms defensively).
- Whether listing requires a prior `calcListingFee` call or extra parameters.

---

## Holdings maintenance passes

Source: [collectorcrypt/trader/holdings.py](../collectorcrypt/trader/holdings.py),
[collectorcrypt/trader/executor.py](../collectorcrypt/trader/executor.py)

Run at the end of every **live** cycle (after status sync + exit pass). Skipped
while the kill switch is tripped — except the read-only market re-check.

Each pass selects candidates with pure logic in `holdings.py`, then asks the
executor to act:

| Pass | Trigger | Action | Endpoint |
|------|---------|--------|----------|
| **Offer reprice** | `dynamic_bidding_enabled()` — runs before orders are sent | Re-quote planned offers against the live order book | `GET card-activity/{nft}` |
| **Offer bump** | `should_bump()` — offer aged past threshold | Re-price up by `TRADER_OFFER_BUMP_USD` (skipped if we already lead) | `marketplace/update-offer` |
| **Offer cancel** | `should_cancel_offer()` — bumps exhausted | Withdraw offer, refund escrow | `marketplace/cancel-offer` |
| **Listing markdown** | `is_due_for_markdown()` — listed unsold | Step price toward cost-basis floor (jittered timing + size, gas-guarded) | `marketplace/update-listing` |
| **Offer accept** | `is_due_for_offer_accept()` — at floor long enough | Accept best incoming bid | `marketplace/accept-offer` |
| **Market re-check** | always | Read-only; updates `oraclePrice` per card | `cards/{wallet}` |

Verified client methods in [collectorcrypt/trader/ccapi.py](../collectorcrypt/trader/ccapi.py):

| Method | Endpoint | Notes |
|--------|----------|-------|
| `get_card_activity(nft, day)` | `GET card-activity/{nft}?day=60&v2=true` | retryable read |
| `update_listing(nft, price, wallet)` | `marketplace/update-listing` | non-retryable write |
| `accept_offer(nft, buyer, price, wallet)` | `marketplace/accept-offer` | non-retryable write |

**Safe-failure:** any signing/broadcast/API error leaves that single order
untouched and the batch continues. Never auto-retried.

Cycle report gains: `offer_pricing` (only when dynamic bidding is on),
`bumped`, `cancelled`, `marked_down`, `offers_accepted`, `market_recheck`.

Strategy source: [collectorcrypt/trader/strategy.py](../collectorcrypt/trader/strategy.py)
Order model: [collectorcrypt/trader/orders.py](../collectorcrypt/trader/orders.py)

---

## Adaptive strategy (anti-exploitation)

Source: [collectorcrypt/trader/strategy.py](../collectorcrypt/trader/strategy.py),
[collectorcrypt/trader/holdings.py](../collectorcrypt/trader/holdings.py),
[collectorcrypt/trader/engine.py](../collectorcrypt/trader/engine.py)

Four refinements stop the bot from leaking capital or being read by a human
counterparty. **All four default to off** (`0`) so existing setups are
unchanged; the `balanced` and `patient_offers` presets switch them on.

### 1. Dynamic range bidding (escrow-leak fix)

Blind offering locks USDC escrow on bids that can never win. Instead, on each
**live** cycle (before any order is signed), `engine._reprice_offers_dynamically`
re-quotes every planned offer against the card's live activity feed
(`GET card-activity/{nft}`, excluding our own wallet) using the pure
`strategy.dynamic_offer_bid`:

- **Uncontested** → bid the opening lowball
  `ask × (1 − TRADER_OFFER_OPEN_DISCOUNT_PCT/100)`.
- **A competitor sits inside the range** → outbid it by `TRADER_OFFER_INCREMENT_USD`.
- **Winning would breach the ceiling** `ask × (1 − TRADER_OFFER_CEILING_PCT/100)`,
  the per-card cap, the remaining budget, or the resale-profit floor → **skip**
  the card so escrow stays free for winnable bids.

Offers are funded cheapest-first against the remaining budget. Enabled only when
both `TRADER_OFFER_OPEN_DISCOUNT_PCT > 0` **and** `TRADER_OFFER_CEILING_PCT > 0`
(open discount must be the larger number). If the order book cannot be read the
pass falls back to the static `TRADER_OFFER_DISCOUNT_PCT` bid (kept if it still
fits) and otherwise drops the offer. Dry-run/demo have no live order book, so
they treat every card as **uncontested** and quote the opening lowball; those
entries are marked `assumed` in the `offer_pricing` report to flag the
simulation assumption.

### 2. No self-bidding on bumps

`engine._run_offer_bump_pass` now reads the order book first and **skips** the
bump when our own wallet is already the highest bidder — a bump is meant to
re-surface our offer in the owner's notifications, not to raise our own escrow
against ourselves. Fails open: an unreadable feed bumps as before.

### 3. Unpredictable markdowns (anti-snipe)

A perfectly regular "−1 % every 3 days" curve can be waited out. `TRADER_MARKDOWN_JITTER_PCT`
applies a **deterministic** per-card, per-step jitter (`holdings.markdown_jitter_factor`,
SHA-256 based so it is stable across cycles yet varies between cards) of
`±jitter %` to **both** the step timing and the step size. The cost-basis floor
and overall trajectory are unchanged.

### 4. Markdown gas guard

`TRADER_MARKDOWN_MIN_CHANGE_USD` skips any markdown whose price drop is smaller
than the threshold (`holdings.markdown_change_is_meaningful`), so a few-cent cut
never costs more in SOL gas than it is worth. Applies to **markdowns only** —
offer bumps stay intentionally tiny.

