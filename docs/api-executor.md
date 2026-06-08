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
| **Offer bump** | `should_bump()` — offer aged past threshold | Re-price up by `TRADER_OFFER_BUMP_USD` | `marketplace/update-offer` |
| **Offer cancel** | `should_cancel_offer()` — bumps exhausted | Withdraw offer, refund escrow | `marketplace/cancel-offer` |
| **Listing markdown** | `is_due_for_markdown()` — listed unsold | Step price toward cost-basis floor | `marketplace/update-listing` |
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

Cycle report gains: `bumped`, `cancelled`, `marked_down`, `offers_accepted`, `market_recheck`.

Strategy source: [collectorcrypt/trader/strategy.py](../collectorcrypt/trader/strategy.py)
Order model: [collectorcrypt/trader/orders.py](../collectorcrypt/trader/orders.py)
