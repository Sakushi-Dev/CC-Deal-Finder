# Go-Live Checklist — what's left before unattended live trading

> Companion to [live-readiness-plan.md](live-readiness-plan.md). That document is
> the detailed verification route with per-shape evidence; **this** file is the
> short, actionable punch list. Tick items here as they are done and record the
> evidence (signature / balance delta / screenshot) next to each.

**Current verdict (2026-06-07): NOT yet cleared for unattended live operation.**
The reversible escrow path is proven on-chain; the items below are still open.

---

## 0. Snapshot of the current state

| Area | Status |
|------|--------|
| Auth (SIWS / Privy) | ✅ proven live |
| Read paths (market, holdings, oracle price, card-activity) | ✅ proven live |
| Reversible escrow offer (make-offer → sign → broadcast → cancel/refund) | ✅ proven on-chain |
| Full test suite | ✅ green |
| Buy → broadcast → on-chain settle | ❌ never executed live |
| Relist / cancel-listing live | ❌ never executed live |
| Risk limits (spend caps, kill-switch) | ❌ all default to `0` (disabled) |
| Dedicated RPC endpoint | ❌ still public `mainnet-beta` (rate-limited) |
| Monitoring / alerting | ❌ not in place |
| README live claims | ✅ updated (this round) |

---

## 1. Risk configuration — BLOCKER (highest priority)

All caps currently read `0` = **disabled** (verified via `load_config()`). With
real money these MUST be set to sane non-zero values first:

- [ ] `TRADER_MAX_SPEND_PER_CYCLE_USD` — e.g. the price of one card.
- [ ] `TRADER_MAX_SPEND_PER_DAY_USD` — a hard daily ceiling.
- [ ] `TRADER_MAX_OPEN_POSITIONS` — cap concurrent in-flight orders.
- [ ] `TRADER_MAX_CONSECUTIVE_FAILURES` — kill-switch (e.g. `3`).
- [ ] `TRADER_RESERVE_USDC` — USDC the bot may never touch.
- [ ] `TRADER_GAS_RESERVE_SOL` — SOL kept for fees (currently `0.05`, ok).
- [ ] Write down the chosen values and the rationale.

**Recommended safety improvement:** add a guard so live mode **refuses to start
when all risk limits are zero**, so the bot can never run uncapped by accident.
(See live-readiness-plan §7.)

## 2. Connection hardening — BLOCKER

- [ ] Set `TRADER_RPC_URL` to a **dedicated** endpoint (Helius / QuickNode /
      Triton). The public `api.mainnet-beta.solana.com` is rate-limited and
      unfit for a trading loop.
- [ ] Confirm the dedicated RPC works for both balance reads and broadcast.

## 3. Supervised live buy test (irreversible — do tiny & last)

A buy settles instantly and cannot be undone, so verify it once, supervised:

- [ ] Pick the cheapest acceptable listing; set caps so only it can fill.
- [ ] `TRADER_LIVE=true`, run **one** cycle (not the loop).
- [ ] Verify: buy tx built → signed → broadcast → on-chain settle →
      order `CONFIRMED` → relist candidate spawned (if resell configured).
- [ ] Reconcile the wallet balance against the order ledger.
- [ ] Record the signature + balance delta as evidence.

## 4. Relist / exit-flow test

- [ ] With the card owned (from §3), run the exit flow once.
- [ ] Verify `create_listing` → sign → broadcast → listing live → `CONFIRMED`.
- [ ] Cancel the listing in the UI to restore a clean state.
- [ ] Record evidence; mark the `marketplace/list` and `cancel-listing` shapes
      verified in live-readiness-plan §0.

## 5. Operational hardening

- [ ] Observability: structured logs + an alert on halt / failure streak
      (deferred ETAPPE 9 — pull forward before unattended operation).
- [ ] Dedicated RPC verified under load (no rate-limit failures mid-cycle).
- [ ] Review `TRADER_AUTO_RESUME` so a restart never silently arms live.
- [ ] Document single-instance assumption (never two bots on one wallet).
- [ ] Write a short operations runbook: start / stop, how to cancel a stuck
      order, incident response.

## 6. Final gate before unattended live

- [ ] §1–§5 complete with evidence recorded.
- [ ] Full test suite green locally **and** in CI.
- [ ] One supervised live session (smallest caps) run end-to-end without manual
      fixes.
- [ ] Explicit operator sign-off that limits, RPC, and monitoring are in place.

> Only after this gate may the loop run unattended with real funds — and even
> then, start with the smallest viable caps.

---

## Optional / nice-to-have (not blockers)

- [ ] (Optional) Live `update-offer` bump test (`tools/live_offer_check.py
      --phase bump`) — the body is already captured; place+cancel already prove
      sign+broadcast.
- [ ] "Select all / clear" control in the category dropdown.
- [ ] Recommended non-zero risk defaults shipped in
      `trader_settings.example.json`.

## Security reminders (ongoing)

- The wallet **private key** lives only in the git-ignored `.env`
  (`TRADER_WALLET_SECRET`) — never in the UI, the store, or a commit.
- DevTools `.bash` captures under `tools/captures/` contain real wallet/signature/card data
  and are git-ignored — never commit them.
- Use a **dedicated, separately funded** test wallet; treat the key like cash.
