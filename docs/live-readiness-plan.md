# Live-Trading Readiness — Verification Route Plan

> Status: **Beta / dry-run-ready / live-integration in verification.**
> This plan tracks every step required before the bot may spend real money
> unattended. It is the single source of truth for live readiness; update the
> checkboxes and the "Evidence" column as each item is proven.

## Legend

- [x] verified live against the real API (evidence recorded)
- [~] partially verified / assumed-correct but not yet proven end-to-end
- [ ] not started / blocked

Each shape is only "verified" when it has been exercised against the **real**
CollectorCrypt / Privy API and the request **and** response were inspected.

---

## 0. Current verification state (as of 2026-06-07)

| Area | State | Evidence |
|------|-------|----------|
| Privy SIWS init | [x] | `POST auth.privy.io/api/v1/siws/init` → 200 `{nonce,...}` |
| Privy SIWS authenticate | [x] | Captured live request; handshake mints a JWT (base64 sig, `walletClientType:"Phantom"`) |
| CC accepts Privy JWT as Bearer | [x] | `checkListingStatus` returned 200 with the SIWS-minted token |
| `checkListingStatus` (RPC) | [x] | `POST /v2 {method,params}` → 200 `{exists,marketplace,listing}` |
| `marketplace/buy` request body | [x] | 201 → bare base64 VersionedTransaction (probe 2026-06-06) |
| `marketplace/buy` → broadcast → on-chain settle | [ ] | Never executed (requires a real purchase) |
| `marketplace/make-offer` request body | [x] | DevTools capture 2026-06-07: `{cardId,currency,nftAddress,price,wallet}` (cardId = raw card id; old 400 cause) |
| `marketplace/update-offer` request body (bump) | [x] | DevTools capture 2026-06-07: `{buyer,currency,nftAddress,price,wallet}` — real offer edit (answers §9.2) |
| `marketplace/cancel-offer` request body | [x] | DevTools capture 2026-06-07: `{coin,keepInEscrow,nftAddress,wallet}` (field is `coin`, no offer id) |
| `marketplace/broadcast` response shape | [x] | DevTools capture 2026-06-07: `{success:true,signature,message}` |
| `marketplace/list` / relist body | [ ] | Path known, body unverified |
| `cancel-listing` body | [ ] | Path known, body unverified |
| `sign_transaction` (base64 + v0, sole signer) | [~] | Plausible; only provable via a real signed broadcast |
| Status-sync vocabulary (confirmed/cancelled) | [~] | `checkListingStatus` lacks status words → sync stays failure-safe |
| `update-listing` body (markdown / price change) | [ ] | Holdings plan Etappe 8; path known, body unverified |
| `getCardOffers` (incoming offers on a held card) | [ ] | Holdings plan Etappe 8; RPC name known, shape unverified |
| `accept-offer` body | [ ] | Holdings plan Etappe 8; path known, body unverified |
| Authoritative "sold" signal for a held card | [x] | DevTools capture 2026-06-07: `GET cards/{wallet}/` lists only owned cards — absence from the fully-paged owned set = sold/exited (no per-card status). Wired as `ownership_sync`. |
| Current market value of a single owned NFT | [x] | `oraclePrice` per card in the `cards/{wallet}/` response, wired into `_run_market_recheck` (E8.4) |

**Bottom line:** auth and read paths are proven; **no write has settled
on-chain yet**. The remaining unknowns all collapse once one real, reversible
write (an escrow offer) is captured and executed. The post-buy lifecycle adds a
few more write shapes (markdown / accept-offer / sold-signal) tracked in
[holdings-lifecycle-plan.md](holdings-lifecycle-plan.md) Etappe 8 — they are
verified the same way (capture → align → tiny supervised test) and are listed
here so this plan stays the single live-readiness source of truth.

---

## 1. Prerequisites (one-time setup)

- [ ] **Dedicated test wallet** — a fresh Phantom wallet used only for this bot.
- [ ] Fund with a **minimal** amount: e.g. **20 USDC + ~0.06 SOL** for fees.
- [ ] Private key in gitignored `.env` as `TRADER_WALLET_SECRET` (never in UI/store).
- [ ] **Dedicated RPC endpoint** (Helius/QuickNode/Triton) in `TRADER_RPC_URL` —
      the public `api.mainnet-beta.solana.com` default is rate-limited and unfit
      for operation.
- [ ] Confirm `TRADER_LIVE` is **unset/false** until step 5.
- [ ] `git` working tree clean; full test suite green (`pytest tests/ -q`).

---

## 2. Capture the unverified write shapes (DevTools)

The cheapest, safest way to verify the remaining shapes is to capture **one real
reversible action** from the browser, then mirror it in code.

- [ ] In the CC UI, with **F12 → Network → Preserve log**, place a small **offer**
      (escrow bid) on any card, then **cancel** it (USDC refunded).
- [ ] Copy as cURL (redact `authorization` + `cookie`) for each request:
  - [ ] `make-offer` **or** `createMakeOfferTx` (the tx-builder)
  - [ ] `broadcast` (the signed-tx submit) — **captures the broadcast response shape**
  - [ ] `cancel-offer` (the refund path)
- [ ] Record: endpoint, card identifier field, price unit/scale, currency,
      optional expiry, and the exact response envelope.
- [ ] **Post-buy lifecycle shapes** (for the holdings features — capture when a
      held card is available, see [holdings-lifecycle-plan.md](holdings-lifecycle-plan.md)
      Etappe 8): `update-listing` (price change / markdown), `getCardOffers`
      (incoming bids), `accept-offer`, and whichever endpoint authoritatively
      reports a held card as **sold**.

**Why an offer first:** offers sit in escrow and are refundable via cancel, so
this is a fully **reversible** live test — unlike a buy, which settles instantly.

---

## 3. Align code to the captured shapes

- [ ] Update `ccapi.make_offer` body to the verified shape (likely the card's
      internal `id`/`receiptId`, not `nftAddress`; possibly the RPC
      `createMakeOfferTx` instead of the REST path).
- [ ] Update `ccapi.broadcast` **response** parsing (`_extract_signature`,
      `_is_confirmed`, `_is_filled`) to the real envelope.
- [ ] Update `ccapi.cancel_offer` to the verified body.
- [ ] Confirm `wallet.sign_transaction` (base64 + v0 + sole signer) round-trips
      the real offer transaction.
- [ ] Update `docs/api.md`: mark offer/broadcast/cancel as VERIFIED.
- [ ] Add/adjust tests for the new shapes; keep the suite green.

---

## 4. Reversible live offer test (escrow)

Run **outside** the engine, as a controlled two-phase script (no loop):

- [ ] **Phase create**: build the offer tx; inspect the raw response. No signing.
- [ ] **Phase broadcast**: sign locally, broadcast; confirm **USDC moved to escrow**
      and the order reaches `OPEN`.
- [ ] Inspect the broadcast response and the resulting on-chain state.
- [ ] **Cancel the offer in the UI**; confirm the **USDC is refunded**.
- [ ] Verify the reconciler/status-sync interprets the lifecycle correctly.

Exit criteria: an offer was placed, observed in escrow, and refunded — with the
code (not a script) producing the same request bytes.

---

## 5. Reversible-where-possible live buy test (smallest amount)

A buy settles immediately (not reversible), so do this **last** and **tiny**:

- [ ] Pick the cheapest acceptable listing; set a hard cap so only it can fill.
- [ ] Set risk limits (see §7) to a single, minimal purchase.
- [ ] `TRADER_LIVE=true`, run **one** cycle (not the loop).
- [ ] Verify: buy tx built → signed → broadcast → **on-chain settle** →
      order `CONFIRMED` → relist candidate spawned (if resell configured).
- [ ] Verify status-sync confirms the buy on the next pass.
- [ ] Reconcile the wallet balance against the order ledger.

Exit criteria: a real card was bought for a known tiny amount and the full
state machine matched on-chain reality.

---

## 6. Relist / exit-flow test

- [ ] With the card owned (from §5), run the exit flow once.
- [ ] Verify `create_listing` → sign → broadcast → listing live → `CONFIRMED`.
- [ ] Cancel the listing in the UI to restore a clean state.

---

## 7. Mandatory risk configuration for live

All risk limits currently default to `0` (disabled). For real money they MUST be
set before going live:

- [ ] `TRADER_MAX_SPEND_PER_CYCLE_USD` — small (e.g. one card).
- [ ] `TRADER_MAX_SPEND_PER_DAY_USD` — a hard daily ceiling.
- [ ] `TRADER_MAX_OPEN_POSITIONS` — cap concurrent exposure.
- [ ] `TRADER_MAX_CONSECUTIVE_FAILURES` — kill-switch (e.g. 3).
- [ ] `TRADER_RESERVE_USDC` / `TRADER_GAS_RESERVE_SOL` — keep funds untouchable.
- [ ] Document the chosen values and the rationale.

Consider shipping **recommended non-zero defaults** (or a "live refuses to start
with all-zero limits" guard) so an operator cannot accidentally run uncapped.

---

## 8. Operational hardening

- [ ] **Operations checklist** for live mode (start/stop, monitoring, incident
      response, how to cancel a stuck order).
- [ ] Dedicated RPC verified under load (no rate-limit failures mid-cycle).
- [ ] Auto-resume behaviour reviewed (`TRADER_AUTO_RESUME` never silently arms live).
- [ ] Observability: structured logs / alert on halt or failure streak
      (deferred ETAPPE 9 — pull forward before unattended operation).
- [ ] Single-instance assumption documented (no two bots on one wallet).

---

## 9. Documentation reconciliation

- [ ] **README**: remove the stale "LiveExecutor not implemented" claim; describe
      the real live gate (live + signer + auth provider) and its safeguards.
- [ ] Update the test count and capabilities in README.
- [ ] `docs/api.md`: ensure every endpoint reflects VERIFIED vs assumed honestly.
- [ ] Cross-link this plan from README so operators see live status at a glance.
- [ ] Keep this plan and [holdings-lifecycle-plan.md](holdings-lifecycle-plan.md)
      in sync: the post-buy lifecycle write shapes (markdown / accept-offer /
      sold-signal) are verified under Etappe 8 there and mirrored in §0 here.

---

## 10. Final gate before unattended live

- [ ] Steps 1–9 complete with evidence recorded.
- [ ] Full test suite green locally **and** in CI.
- [ ] One supervised live session (small caps) run end-to-end without manual fixes.
- [ ] Explicit operator sign-off that limits, RPC, and monitoring are in place.

Only after this gate may the loop run unattended with real funds — and even then
starting with the smallest viable caps.

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
- Never commit `.env`, `*.db`, history files, or any DevTools capture
  (`auth.log`-style files) — they contain secrets/signatures.
