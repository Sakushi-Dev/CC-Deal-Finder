# CollectorCrypt API — Risk Engine & Crash Recovery

← [Index](index.md)

---

## Risk engine

Source: [collectorcrypt/trader/risk.py](../collectorcrypt/trader/risk.py)

The risk engine is the final gate before any live order is sent — independent of
the planner, so a planning bug or market anomaly cannot drain the wallet.

### When it runs

After planning and **before** the executor on every **live** cycle. Blocked
orders are transitioned to `FAILED` with a `risk gate: …` detail and never reach
the executor. Dry-run/demo cycles are not gated, but the posture is still
computed for display.

### Enforced limits

Each limit is `0` = disabled (existing setups unchanged until an operator opts in).

| Env var | Control |
|---------|---------|
| `TRADER_MAX_CONSECUTIVE_FAILURES` | Kill switch — after N real consecutive failures, **halt all** trading this cycle (skip exit/relist; read-only status sync still runs). |
| `TRADER_MAX_OPEN_POSITIONS` | Cap on concurrent real in-flight orders. |
| `TRADER_MAX_SPEND_PER_CYCLE_USD` | Ceiling on USD committed in one cycle. |
| `TRADER_MAX_SPEND_PER_DAY_USD` | Rolling 24h ceiling on realized spend across cycles. |

Only spending orders (`buy`/`offer`) count against caps; relists (sells) do not.

### Usage sources

Source: [collectorcrypt/trader/store.py](../collectorcrypt/trader/store.py)

| Query | Used for |
|-------|---------|
| `open_position_count()` | Current concurrent active orders |
| `confirmed_spend_since(ts)` | Sum of `price_usd` of confirmed non-simulated buys/offers in last 24h |
| `recent_terminal_statuses()` | Consecutive-failure streak |

### Fail-safe

Any failure to read the risk state resolves to **halt** — zero orders sent.
`RiskEngine.evaluate()` never raises.

### Cycle report

`risk` block: `enabled`, `halted`, `limits`, `usage`,
`cycle.{planned_spend, allowed, blocked}`, `breaches`.

Manager snapshot exposes a read-only `risk` posture so the dashboard shows caps,
today's spend, open positions and kill-switch state even before a cycle runs.

### Open / unverified (risk)

- "Spend" counts a confirmed offer's full `price_usd` as realized; whether an
  accepted offer settles for exactly the bid is assumed.
- Daily window is a simple rolling 24h on `created_at`; does not align to a
  wallet/exchange settlement day.

---

## Crash recovery / auto-resume

Source: [collectorcrypt/trader/manager.py](../collectorcrypt/trader/manager.py),
[collectorcrypt/trader/store.py](../collectorcrypt/trader/store.py)

### Persisted loop state

Every loop control change (`start_loop`, `pause`, `resume`, `stop`) writes
`{loop_active, paused, interval}` to a `runtime` key/value table in the store
(`set_runtime` / `get_runtime`). Holds **no secrets** — only public control flags.

### Startup reconcile

On construction the manager runs a single read-only reconciliation so the UI
immediately reflects any orders that were in flight when the process stopped.
Never submits, signs or cancels anything.

Source: [collectorcrypt/trader/reconcile.py](../collectorcrypt/trader/reconcile.py)

### Opt-in auto-resume

Only when `TRADER_AUTO_RESUME=true` **and** the loop was active before the
restart is the worker restarted. Like `TRADER_LIVE`, the flag is read **from
the environment only** (never from the UI overrides file) — a crash can never
silently arm trading.

### Manager recovery snapshot

| Field | Meaning |
|-------|---------|
| `performed` | Recovery ran at startup. |
| `in_flight` | Active orders found by the startup reconcile. |
| `was_active` | The loop was active in the persisted state. |
| `auto_resume` | `TRADER_AUTO_RESUME` is set. |
| `resumed` | The loop was actually restarted. |

### Fail-safe

Persistence and recovery are best-effort and never block control flow or startup
— a store error leaves the in-memory state authoritative and defaults
auto-resume to *off*.

### Open / unverified (recovery)

- Auto-resume restores the loop in the persisted mode (e.g. paused stays paused);
  does not retroactively run cycles missed while the process was down.
- A still-running second instance pointing at the same store could both resume;
  single-instance operation is assumed.

---

## Settings reference

Source: [collectorcrypt/trader/settings.py](../collectorcrypt/trader/settings.py)

UI-editable fields, built-in presets, and user profiles. Security variables
(`TRADER_WALLET_KEY`, `TRADER_LIVE`) stay in `.env` only and are never exposed
through the settings UI.

Example: [trader_settings.example.json](../trader_settings.example.json)
