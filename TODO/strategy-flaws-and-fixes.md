# Critical Strategy Flaws & Fixes

This document outlines critical blind spots identified in the autonomous trading strategy and how they should be addressed to prevent capital inefficiency and exploitation.

> **Status: ✅ All four fixes implemented.** Every new tunable defaults to `0`
> (off) so existing setups stay unchanged; the *Balanced* and *Patient offers*
> presets enable them. See [docs/api-executor.md](../docs/api-executor.md)
> ("Adaptive strategy") for the live mechanics.
>
> | # | Fix | Final settings | Code |
> |---|-----|----------------|------|
> | 1 | Dynamic range bidding | `TRADER_OFFER_OPEN_DISCOUNT_PCT`, `TRADER_OFFER_CEILING_PCT`, `TRADER_OFFER_INCREMENT_USD` | `strategy.dynamic_offer_bid`, `engine._reprice_offers_dynamically` |
> | 2 | No self-bidding on bumps | *(no new field)* | `engine._run_offer_bump_pass` |
> | 3 | Unpredictable markdowns | `TRADER_MARKDOWN_JITTER_PCT` | `holdings.markdown_jitter_factor` |
> | 4 | Markdown gas guard | `TRADER_MARKDOWN_MIN_CHANGE_USD` | `holdings.markdown_change_is_meaningful` |
>
> **Naming note:** flaw 1 shipped as `OPEN_DISCOUNT_PCT` / `CEILING_PCT` (clearer
> than the originally proposed `MIN_PCT` / `MAX_PCT`); the *open discount* is the
> larger number (lowest opening price), the *ceiling discount* the smaller (the
> highest price we will pay). The static `TRADER_OFFER_DISCOUNT_PCT` is retained
> as the fallback when the order book is unreadable or in dry-run/demo.

## 1. Escrow Capital Leak (Blind Offering)

**The Problem:**
Currently, the bot places offers strictly based on `ask_usd * offer_factor` without checking the current order book. If another user already has a higher bid, our USDC is locked in escrow indefinitely with zero chance of being accepted, draining our available volume for real deals.

**The Solution: Dynamic Range Bidding**
- **New Settings:** Introduce an offer range (`TRADER_OFFER_MIN_PCT` and `TRADER_OFFER_MAX_PCT` below ask) and an increment value (`TRADER_OFFER_INCREMENT_USD`, e.g., 0.01).
- **Logic:** Before placing an offer, query the highest existing bid.
    - If the highest bid is below our `MIN_PCT` threshold, place our bid at `MIN_PCT`.
    - If the highest bid is within our `MIN` - `MAX` range, bid `highest bid + TRADER_OFFER_INCREMENT_USD`.
    - If the highest bid is above our `MAX_PCT` threshold, **skip** the card entirely to save escrow volume.

## 2. Bidding Against Ourselves (Offer Bumps)

**The Problem:**
The `should_bump` logic increments aged offers purely based on time and `bump_count`. It does not check if we are already the highest bidder. If we are, we are needlessly raising our own price and giving away money.

**The Solution:**
- **Logic:** Before bumping any offer, check the highest active bid. Only execute the bump if we are *not* currently the highest bidder.

## 3. The "Wait-It-Out" Exploit (Predictable Markdowns)

**The Problem:**
The markdown logic is deterministic (e.g., exactly -1% every exactly 24 hours). Other actors or bots can easily reverse-engineer this pattern and simply wait for the item to reach the cost floor.

**The Solution: Jittering**
- Introduce randomness to both the markdown interval and the markdown step size.
- Instead of exactly 24 hours, use `24h +/- X hours`.
- Instead of exactly 1%, use `0.8% - 1.2%`. This destroys the predictability of the bot's pricing.

## 4. Death by Gas Fees (SOL Drain)

**The Problem:**
Frequent micro-adjustments (bumping offers by pennies, dropping prices by cents, mass-canceling aged offers) cost Solana gas fees. In a slow market, the bot might bleed SOL without executing profitable trades.

**The Solution:**
- Ensure `TRADER_OFFER_INCREMENT_USD` and markdown steps are large enough to justify the transaction cost. Consider adding a minimum absolute USD threshold for price changes (e.g., don't trigger a blockchain tx for a $0.02 adjustment).
