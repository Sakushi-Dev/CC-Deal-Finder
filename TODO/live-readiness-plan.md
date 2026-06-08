# Live-Trading Readiness — verification route & go-live checklist

> Status: **Beta / dry-run-ready / live-integration in verification.**
> Single source of truth for live readiness. This merges the former
> `go-live-checklist.md` (the short punch list) and the detailed verification
> route. Update the checkboxes and the **Evidence** column as each item is proven.

**Current verdict (2026-06-08): NOT yet cleared for unattended live operation.**
Auth, all read paths, and the reversible escrow offer path are proven on-chain;
the items in [Required before go-live](#required-before-go-live) remain open.

## Legend

- [x] verified live against the real API (evidence recorded)
- [~] partially verified / assumed-correct but not proven end-to-end
- [ ] not started / blocked

Each shape is only "verified" when it has been exercised against the **real**
CollectorCrypt / Privy API with the request **and** response inspected.

---

## 0. Current verification state

### What is already proven (no further action)

| Area | State | Evidence |
|------|-------|----------|
| Privy SIWS init | [x] | `POST auth.privy.io/api/v1/siws/init` → 200 `{nonce,...}` |
| Privy SIWS authenticate | [x] | Captured live; handshake mints a JWT (base64 sig, `walletClientType:"Phantom"`) |
| CC accepts Privy JWT as Bearer | [x] | `checkListingStatus` returned 200 with the SIWS-minted token |
| `checkListingStatus` (RPC) | [x] | `POST /v2 {method,params}` → 200 `{exists,marketplace,listing}` |
| `marketplace/buy` request body | [x] | 201 → bare base64 VersionedTransaction (probe 2026-06-06) |
| `marketplace/make-offer` body | [x] | Settled on-chain 2026-06-07 (sig `5Smokh…d22X`): `{cardId,currency,nftAddress,price,wallet}` |
| `marketplace/update-offer` body (bump) | [x] | DevTools capture 2026-06-07: `{buyer,currency,nftAddress,price,wallet}` |
| `marketplace/cancel-offer` body | [x] | Settled on-chain 2026-06-07 (escrow refunded, sig `4gF8zA…mzCC`): `{coin,keepInEscrow,nftAddress,wallet}` |
| `marketplace/broadcast` response | [x] | Exercised live 2026-06-07 (offer place + cancel): `{success:true,signature,message}` |
| `sign_transaction` (base64 + v0, sole signer) | [x] | Proven 2026-06-07: signed real make-offer + cancel-offer v0 txs locally; both broadcast |
| `update-listing` body (markdown) | [x] | DevTools capture 2026-06-07 (E8.3): `{coin,newPrice,seller,tokenMint,wallet}` — bare base64 tx |
| `card-activity` feed (incoming offers) | [x] | DevTools capture 2026-06-07 (E8.3): `GET card-activity/{nft}?day=60&v2=true` newest-first |
| `accept-offer` body | [x] | DevTools capture 2026-06-07 (E8.3): `{buyer,currency,nftAddress,price,wallet}` — bare base64 tx |
| `marketplace/list` body (relist) | [x] | DevTools capture 2026-06-08: `{cardId,currency,nftAddress,price,wallet}` — bare base64 tx |
| `cancel-listing` body | [x] | DevTools capture 2026-06-08: `{coin,seller,tokenMint,wallet}` — bare base64 tx |
| Authoritative "sold" signal | [x] | `GET cards/{wallet}/` lists only owned cards — absence = sold/exited. Wired as `ownership_sync` |
| Current market value of an owned NFT | [x] | `oraclePrice` per card in `cards/{wallet}/`, wired into `_run_market_recheck` (E8.4) |
| Full test suite | [x] | 846 tests green |
| Audit trail (bot log + transaction CSV) | [x] | `logs/bot.log` + `records/transactions.csv`, append-only, real trades only |
| README live claims | [x] | Live gate + safeguards documented; stale "not implemented" claim removed |

### What is still open

| Area | State | Note |
|------|-------|------|
| `marketplace/buy` → broadcast → on-chain settle | [ ] | Never executed (requires a real purchase — irreversible) |
| Status-sync vocabulary (confirmed/cancelled) | [~] | `checkListingStatus` lacks status words → sync stays failure-safe |
| Risk limits (spend caps, kill-switch) | [ ] | All default to `0` (disabled) |
| Dedicated RPC endpoint | [ ] | Still public `mainnet-beta` (rate-limited) |
| Monitoring / alerting | [ ] | Not in place |

> The post-buy lifecycle write shapes (markdown / accept-offer / sold-signal) are
> verified under Etappe 8 in
> [holdings-lifecycle-plan.md](holdings-lifecycle-plan.md) and mirrored above so
> this stays the single live-readiness source of truth.

---

## Required before go-live

These are **blockers**. The loop may not run unattended with real funds until
every item here is done with evidence recorded.

### R1. Prerequisites (one-time setup)

- [ ] **Dedicated test wallet** — a fresh Phantom wallet used only for this bot.
- [ ] Fund with a **minimal** amount: e.g. **20 USDC + ~0.06 SOL** for fees.
- [ ] Private key in gitignored `.env` as `TRADER_WALLET_SECRET` (never in UI/store).
- [ ] Confirm `TRADER_LIVE` is **unset/false** until R4.
- [ ] `git` tree clean; full test suite green (`pytest tests/ -q`).

### R2. Risk configuration — highest priority

All caps currently read `0` = **disabled** (verified via `load_config()`). With
real money these MUST be set to sane non-zero values first:

- [ ] `TRADER_MAX_SPEND_PER_CYCLE_USD` — e.g. the price of one card.
- [ ] `TRADER_MAX_SPEND_PER_DAY_USD` — a hard daily ceiling.
- [ ] `TRADER_MAX_OPEN_POSITIONS` — cap concurrent in-flight orders.
- [ ] `TRADER_MAX_CONSECUTIVE_FAILURES` — kill-switch (e.g. `3`).
- [ ] `TRADER_RESERVE_USDC` — USDC the bot may never touch.
- [ ] `TRADER_GAS_RESERVE_SOL` — SOL kept for fees (currently `0.05`, ok).
- [ ] Write down the chosen values and the rationale.

**Recommended guard (DONE 2026-06-08):** live mode now **refuses to start when all risk limits are
zero** (`live_caps_configured()` in `risk.py`; wired in `engine.run_cycle`). The
engine returns a halted posture and blocks all orders without sending anything.
`trader_settings.example.json` ships with non-zero defaults (cycle=$30, day=$100,
open=3, failures=3). 853 tests green.

### R3. Connection hardening

- [ ] Set `TRADER_RPC_URL` to a **dedicated** endpoint (Helius / QuickNode /
      Triton). The public `api.mainnet-beta.solana.com` default is rate-limited
      and unfit for a trading loop.
- [ ] Confirm the dedicated RPC works for both balance reads and broadcast.
- [ ] Verify the RPC under load — no rate-limit failures mid-cycle.

### R4. Supervised live buy test (irreversible — do tiny & last)

A buy settles instantly and cannot be undone, so verify it once, supervised, with
caps set so only the cheapest listing can fill:

- [ ] Pick the cheapest acceptable listing; set caps so only it can fill.
- [ ] `TRADER_LIVE=true`, run **one** cycle (not the loop).
- [ ] Verify: buy tx built → signed → broadcast → on-chain settle →
      order `CONFIRMED` → relist candidate spawned (if resell configured).
- [ ] Verify status-sync confirms the buy on the next pass.
- [ ] Reconcile the wallet balance against the order ledger.
- [ ] Record the signature + balance delta as evidence; mark `marketplace/buy →
      settle` verified in §0.

### R5. Relist / exit-flow test

- [ ] With the card owned (from R4), run the exit flow once.
- [ ] Verify `create_listing` → sign → broadcast → listing live → `CONFIRMED`.
- [ ] Cancel the listing in the UI to restore a clean state.
- [ ] Record evidence; mark the `marketplace/list` and `cancel-listing` shapes
      verified in §0.

### R6. Operational hardening

- [ ] Observability: structured logs + an alert on halt / failure streak.
      (The audit trail — `logs/bot.log` + `records/transactions.csv` — is in
      place; alerting on top of it is still missing.)
- [ ] Review `TRADER_AUTO_RESUME` so a restart never silently arms live.
- [ ] Document the single-instance assumption (never two bots on one wallet).
- [ ] Write a short operations runbook: start / stop, how to cancel a stuck
      order, incident response.

### R7. Final gate before unattended live

- [ ] R1–R6 complete with evidence recorded.
- [ ] Full test suite green locally **and** in CI.
- [ ] One supervised live session (smallest caps) run end-to-end without manual
      fixes.
- [ ] Explicit operator sign-off that limits, RPC, and monitoring are in place.

> Only after this gate may the loop run unattended with real funds — and even
> then, start with the smallest viable caps.

---

## Optional / nice-to-have (not blockers)

- [ ] **Live `update-offer` bump test** (`tools/live_offer_check.py --phase bump`)
      — the body is already captured; place + cancel already prove sign+broadcast,
      so this only exercises the edit path.
- [ ] Verify the reconciler/status-sync interprets a full offer lifecycle
      end-to-end (place → bump → cancel).
- [ ] Recommended non-zero risk defaults shipped in
      `trader_settings.example.json`. **(DONE 2026-06-08: cycle=$30, day=$100, open=3, failures=3)**
- [ ] **"Select all / clear"** control in the category dropdown. **(DONE 2026-06-08)**

---

## Reference — how the reversible escrow path was proven

Run **outside** the engine via the controlled harness
[tools/live_offer_check.py](../tools/live_offer_check.py) — it drives the real
`ccapi` request builders + `wallet.sign_transaction` (the same bytes the
`LiveExecutor` emits), one phase per invocation (no loop). Any fund-moving phase
refuses to run without `--confirm-funds` and prints USDC/SOL before & after.

- [x] **Phase create** — build the offer tx; no signing, no funds move.
      *(2026-06-07: SIWS auth + make-offer body accepted; returns a signable
      base64 tx. NOTE: absolute minimum-offer floor ~$5 USDC — a bid ≤ $1 is
      rejected `400 "Too low"`.)*
- [x] **Phase place** (`--confirm-funds`) — sign locally, broadcast; USDC moved
      to escrow. *(2026-06-07: real 6 USDC offer on nft `AhUaju…qKkH`, ask 18;
      sig `5Smokh…d22X`; USDC 20.005 → 14.005 (−6), SOL −0.002 gas.)*
- [x] **Phase cancel** (`--confirm-funds`) — USDC refunded. *(2026-06-07: sig
      `4gF8zA…mzCC`; USDC 14.005 → 20.005 (+6 refunded). Reversible round-trip
      complete.)*

This proved `sign_transaction` (base64 + v0 + sole signer) and the broadcast
response shape end-to-end. The buy / list / cancel-listing write shapes (R4, R5)
are verified the same way: capture → align code → tiny supervised test.

---

## Risk notes

- A **buy is irreversible**; an **offer is reversible** (escrow + cancel). Always
  verify new write shapes via offers first.
- Writes are **never auto-retried** (double-spend guard) — a transient failure
  marks the order `FAILED` for the operator, by design.
- Status-sync via `checkListingStatus` cannot currently auto-confirm a buy (the
  endpoint carries no confirmed/cancelled vocabulary); this is failure-safe
  (stays `unresolved`) but means confirmation relies on the broadcast response
  until a richer status source is verified.

## Security reminders (ongoing)

- The wallet **private key** lives only in the git-ignored `.env`
  (`TRADER_WALLET_SECRET`) — never in the UI, the store, or a commit.
- DevTools captures under `tools/captures/` (and any `*.ini` / `auth.log`-style
  files) contain real wallet / signature / card data and are git-ignored — never
  commit them.
- The audit trail (`logs/`, `records/`) contains real trade data and is
  git-ignored — never commit it.
- Use a **dedicated, separately funded** test wallet; treat the key like cash.
