# CollectorCrypt API — Authentication

← [Index](index.md)

---

## Session providers

Source: [collectorcrypt/trader/auth.py](../collectorcrypt/trader/auth.py)

| Provider | Description |
|----------|-------------|
| `NullSessionProvider` | Default. Owns no credentials, always refuses. No authenticated request can be sent. |
| `StaticTokenProvider` | Wraps a pre-obtained token (`TRADER_CC_TOKEN`) for integration testing. |
| `PrivySiwsProvider` | Real Sign-In-With-Solana handshake (see below). |

The token is sent as `Authorization: Bearer <token>`, held only in memory and
**redacted from all logs**. Never written to the order store.

Factory: `make_session_provider()` in
[collectorcrypt/trader/siws.py](../collectorcrypt/trader/siws.py)

Live-readiness gate: `siws.check_live_ready()` — live trading requires *all* of
`TRADER_LIVE=true`, a signing wallet, a non-`none` auth provider, and a session
that can be established now. Any missing precondition raises `CCAuthError`.

---

## Privy SIWS handshake ✅ VERIFIED (2026-06-06)

> Verified end-to-end on 2026-06-06. Key finding: **SIWS runs on Privy's own
> API host `https://auth.privy.io`**, not on `api.collectorcrypt.com` (which
> returns 404 for these paths).

Source: [collectorcrypt/trader/siws.py](../collectorcrypt/trader/siws.py)

### Public environment values (from the frontend bundle — not secrets)

| Value | |
|-------|-|
| Privy auth host | `https://auth.privy.io` |
| `PRIVY_APP_ID` | `cmdgt21w400lgky0mkn069jui` |
| `PRIVY_CLIENT_ID` | `client-WY6NvtFJDWADQMppqbxv6hSrGa1igpPo8eVK9DfhnSGTi` |
| `NETWORK` | `mainnet` |

### Required headers

Privy enforces a CORS-style check — without `Origin` it returns `403 missing_origin`.

```
Content-Type: application/json
Origin: https://collectorcrypt.com
Referer: https://collectorcrypt.com/
privy-app-id: cmdgt21w400lgky0mkn069jui
privy-client-id: client-WY6NvtFJDWADQMppqbxv6hSrGa1igpPo8eVK9DfhnSGTi
privy-client: react-auth:3.28.0
privy-ca-id: <per-device uuid v4>
```

### Step 1 — init ✅ VERIFIED

`POST https://auth.privy.io/api/v1/siws/init`

```jsonc
{ "address": "<wallet>" }
```

Response **HTTP 200**:

```jsonc
{ "nonce": "<64-char hex>", "address": "<wallet>",
  "expires_at": "2026-06-06T21:12:16.666Z" }  // nonce TTL ~10 min
```

### Step 2 — sign (local)

The client builds the SIWS message locally (Privy returns no pre-built message)
and signs it via [collectorcrypt/trader/wallet.py](../collectorcrypt/trader/wallet.py).

⚠️ **Signature must be base64-encoded** (`Wallet.sign_message(..., encoding="base64")`),
not base58. This was the original cause of the `400` error.

**Exact Privy Solana message template:**

```
collectorcrypt.com wants you to sign in with your Solana account:
<address>

You are proving you own <address>.

URI: https://collectorcrypt.com
Version: 1
Chain ID: mainnet
Nonce: <nonce>
Issued At: <ISO8601 millis Z>
Resources:
- https://privy.io
```

### Step 3 — authenticate ✅ VERIFIED

`POST https://auth.privy.io/api/v1/siws/authenticate`

```jsonc
{ "message": "<full SIWS message>",
  "signature": "<base64 signature>",
  "walletClientType": "Phantom",
  "connectorType": "solana_adapter",
  "mode": "login-or-sign-up",
  "message_type": "plain" }
```

Note: `walletClientType` is capitalised `"Phantom"`. There is **no** `address`/`nonce`
field — the nonce travels inside the signed message.

Response **HTTP 200** — bearer JWT + `account_id` (`did:privy:<id>`). That JWT
is accepted directly by the CC trading API:

> **CC trading API accepts the Privy bearer token directly — ✅ VERIFIED.**
> `Authorization: Bearer <jwt>` accepted by `api.collectorcrypt.com`.
> No CC-side exchange required.

**There is no `users/me` on CC API** — `/api/v1/users/me` is a Privy path
(returns 404 on CC). Account identity comes from the wallet address.

### Open / unverified (SIWS)

- Exact token + expiry JSON keys in the authenticate response were not captured
  directly; `_extract_token`/`_extract_expiry` read common Privy shapes defensively.
- Refresh strategy: no separate refresh endpoint; the provider re-runs the full
  SIWS handshake on expiry (wallet re-signs).

---

## Captures

Reference DevTools captures:
- [tools/captures/requests/accept_offer.bash](../tools/captures/requests/accept_offer.bash)
- [tools/captures/responses/accept_offer_response.bash](../tools/captures/responses/accept_offer_response.bash)
