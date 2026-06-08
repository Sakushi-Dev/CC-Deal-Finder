# CollectorCrypt API — Trading Flows

← [Index](index.md)

> All flows below are **verified** against live probes (2026-06-06 / 2026-06-07).
> Transport source: [collectorcrypt/trader/ccapi.py](../collectorcrypt/trader/ccapi.py)

---

## Transport overview

Auth: `Authorization: Bearer <privy-jwt>` + `Origin: https://collectorcrypt.com`.
See [api-auth.md](api-auth.md) for how to obtain the token.

| Style | HTTP | URL | Used by |
|-------|------|-----|---------|
| REST POST | POST | `<path>` | buy, broadcast, make-offer, list, cancel-*, accept-offer |
| RPC `/v2` | POST | `/v2` — body `{method, params}` | `checkListingStatus` |
| RPC root | POST | `/` — body `{method, params}` | `createQuickBuyTx`, `createMakeOfferTx`, etc. |

---

## Buy flow

### 1. Prepare ✅ VERIFIED HTTP 201

`POST https://api.collectorcrypt.com/marketplace/buy`

```jsonc
{ "currency": "USDC", "nftAddress": "<nft>", "price": 900,
  "wallet": "<buyer wallet>", "fundingSource": "wallet" }
```

- `fundingSource`: `"wallet"` (direct) or `"escrow"`.
- **No `receiptId`** in the buy body (old assumption was wrong).
- Response is a **bare base64 `VersionedTransaction` string** (no JSON envelope).

DevTools captures: [requests/](../tools/captures/requests/) [responses/](../tools/captures/responses/)

### 2. Sign (local)

[collectorcrypt/trader/wallet.py](../collectorcrypt/trader/wallet.py) — `Wallet.sign_transaction()`.
Base64-decode → `solders` `VersionedTransaction` → re-encode base64.
No private key ever leaves the process.

### 3. Broadcast ✅ VERIFIED HTTP 200

`POST marketplace/broadcast`

```jsonc
{ "signedTransaction": "<base64 signed tx>", "wallet": "<wallet>",
  "nftAddress": "<nft, optional>" }
```

Response:

```jsonc
{ "success": true, "signature": "<sig>",
  "message": "Transaction broadcast successfully" }
```

⚠️ **Never retried** — a second broadcast could double-spend.

Capture: [requests/broadcast.bash](../tools/captures/requests/broadcast.bash),
[responses/broadcast_response.bash](../tools/captures/responses/broadcast_response.bash)

---

## Listing status ✅ VERIFIED HTTP 200

`POST https://api.collectorcrypt.com/v2`

```jsonc
{ "method": "checkListingStatus",
  "params": { "nftAddress": "<nft>", "wallet": "<wallet>" } }
```

`params` is object-strict: exactly `nftAddress` + `wallet`. Any extra key is rejected.

Response:
```jsonc
{ "exists": bool, "marketplace": "string|null", "listing": "object|null" }
```

---

## Offer lifecycle ✅ VERIFIED (DevTools capture 2026-06-07)

All offer endpoints return a **bare base64 transaction** to sign + broadcast.
Source: [collectorcrypt/trader/executor.py](../collectorcrypt/trader/executor.py)

### Make offer ✅ VERIFIED

`POST marketplace/make-offer`

```jsonc
{ "cardId": "<raw CC card id, e.g. 2024122019C5785>", "currency": "USDC",
  "nftAddress": "<nft>", "price": 8, "wallet": "<bidder wallet>" }
```

`cardId` is the card's internal CC `id` (top-level field) and is **required**.
Old body `{ nftAddress, price, currency }` was rejected with HTTP 400.

Capture: [requests/make_offer.bash](../tools/captures/requests/make_offer.bash),
[responses/make_offer_response.bash](../tools/captures/responses/make_offer_response.bash)

### Update offer (bump) ✅ VERIFIED

`POST marketplace/update-offer`

```jsonc
{ "buyer": "<bidder wallet>", "currency": "USDC", "nftAddress": "<nft>",
  "price": 9, "wallet": "<bidder wallet>" }
```

`buyer` and `wallet` are both the bidder's address. Bumping is a real edit
(re-notifies the owner) — not cancel+remake.

Capture: [requests/update_offer.bash](../tools/captures/requests/update_offer.bash),
[responses/update_offer_response.bash](../tools/captures/responses/update_offer_response.bash)

### Cancel offer ✅ VERIFIED

`POST marketplace/cancel-offer`

```jsonc
{ "coin": "USDC", "keepInEscrow": false, "nftAddress": "<nft>",
  "wallet": "<bidder wallet>" }
```

Currency field is **`coin`** (not `currency`). **No offer id** — keyed by
`nftAddress` + `wallet`. Old body `{ "id": <offer_id> }` was wrong.

Capture: [requests/cancel_offer.bash](../tools/captures/requests/cancel_offer.bash),
[responses/cancel_offer_response.bash](../tools/captures/responses/cancel_offer_response.bash)

---

## Owned cards / sold signal ✅ VERIFIED HTTP 200

`GET cards/{wallet}/?page=1&step=96&orderBy=dateDesc`

Response: `{ totalCards, totalPages, filterNFtCard: [...cards] }`

Each card: `nftAddress`, `id`, `listing` (object | `null`), `listedAt`,
`status`, **`oraclePrice`** (market value per card).

**Sold signal:** endpoint lists *only cards still owned*. A card that has sold
is simply **absent** from `filterNFtCard`. Absence from the fully-paged owned
set = authoritative sold/exited signal.

Source: [collectorcrypt/trader/reconcile.py](../collectorcrypt/trader/reconcile.py),
[collectorcrypt/trader/holdings.py](../collectorcrypt/trader/holdings.py)

---

## Card activity feed ✅ VERIFIED HTTP 200

`GET card-activity/{nft}?day=60&v2=true`

Returns a flat newest-first JSON array (wrapped as `{ "data": [...] }`).
Per entry: `action` (`"Offer Made"` | `"Offer Cancelled"` | `"Offer Accepted"` |
`"List"` | `"Listing Updated"`), `amount`, `from`/`to`, `cardId`, `createdAt`,
`id` (= Solana tx signature).

**No standing-offers endpoint and no offer id.** Best active bid reconstructed
by `best_active_offer()` in [collectorcrypt/trader/holdings.py](../collectorcrypt/trader/holdings.py):
per `from.wallet`, keep the newest event; drop wallets whose newest event is a
cancel/accept; pick highest `amount` among open `"Offer Made"`.

---

## Update listing (markdown) ✅ VERIFIED

`POST marketplace/update-listing`

```jsonc
{ "coin": "USDC", "newPrice": 140, "seller": "<wallet>",
  "tokenMint": "<nft>", "wallet": "<wallet>" }
```

`seller` and `wallet` are both our own address. Old assumed body
`{ nftAddress, price, currency }` was wrong on every field. Returns bare base64 tx.

Capture: [requests/update_listing.bash](../tools/captures/requests/update_listing.bash),
[responses/update_listing_response.bash](../tools/captures/responses/update_listing_response.bash)

---

## Accept offer ✅ VERIFIED

`POST marketplace/accept-offer`

```jsonc
{ "buyer": "<bidder wallet>", "currency": "USDC", "nftAddress": "<nft>",
  "price": <offer amount>, "wallet": "<our wallet>" }
```

Offer referenced by `buyer` + `price` + `nftAddress` (no offer id). Maps 1:1
from `best_active_offer` feed result. Returns bare base64 tx.

Capture: [requests/accept_offer.bash](../tools/captures/requests/accept_offer.bash),
[responses/accept_offer_response.bash](../tools/captures/responses/accept_offer_response.bash)

---

## Error & retry policy

Source: [collectorcrypt/trader/ccapi.py](../collectorcrypt/trader/ccapi.py)

| Outcome | Mapped error | Retried automatically? |
|---------|-------------|------------------------|
| 401 / 403 | `CCAuthError` | no (session invalidated) |
| 429 | `CCRateLimitError` | yes, honouring `Retry-After` (reads only) |
| 4xx | `CCClientError` | no |
| 5xx | `CCServerError` | yes, backoff (reads only) |
| network/timeout | `CCNetworkError` | yes, backoff (reads only) |

State-changing calls (`buy`/`make-offer`/`list`/`broadcast`/`cancel-*`) are
**never** auto-retried — a silent retry could double-spend.

---

## Open / unverified

- `checkListingStatus` status vocabulary when a listing *is* active.
- Exact field names and casing of every POST body (camelCase assumed).
- Whether `price` is USD, USDC base units, or lamports for SOL listings.
- The transaction encoding expected by `broadcast` (base64 vs base58).
- Whether offers/listings return their own id immediately or only after a
  follow-up sync.
- Rate-limit headers actually emitted by the API.
