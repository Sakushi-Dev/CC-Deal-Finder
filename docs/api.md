# CollectorCrypt – unofficial API notes

> **As of:** 2026-06-01 · Bundle `main.97af84c3de44d9b7884c.js`
>
> This documentation is **purely reverse-engineered** from the public
> frontend bundle and is not official. Endpoints can change at any time.
> Use [tools/discover_endpoints.py](../tools/discover_endpoints.py) to
> regenerate the list.

## Basics

- **Base URL:** `https://api.collectorcrypt.com`
- **Format:** JSON
- **Auth:** Some endpoints are public (marketplace listings); others require
  a bearer/cookie token from the Privy login (`/api/v1/...authenticate`).
  Without a token → `401 Unauthorized`.
- **Frontend ↔ API:** The React SPA client wraps every call in small
  helper functions (`(0,x.Jt)(path)` = GET, `(0,x.bE)(path,body)` = POST/PUT).
  Paths from the bundle are resolved relative to the base URL.

---

## Confirmed public endpoints

### `GET /marketplace`

Returns paginated listings for a category. This app uses this endpoint.

**Query parameters**

| Name                  | Required | Example         | Description |
|-----------------------|----------|-----------------|-------------|
| `cardType`            | yes      | `Card`          | one of `Card`, `Comic`, `ComicRaw`, `Game`, `Merch`, `Raw`, `Sealed` |
| `page`                | yes      | `1`             | 1-based page index |
| `step`                | yes      | `30`            | cards per page (up to 100 in the UI) |
| `search`              | no       | `charizard`     | full-text search; `+`, `&`, `#` must be URL-encoded |
| `autographed`         | no       | `true`          | autographed cards only |
| `authenticated`       | no       | `true`          | authenticated cards only |
| `marketplaceStatus`   | no       | `Listed,Sold`   | comma list; only these listing statuses |
| `marketplaceTags`     | no       | `Promo`         | comma list of tags |
| `insuredValueMin`     | no       | `100`           | minimum insured value (USD) |
| `insuredValueMax`     | no       | `1000`          | maximum insured value (USD) |

**Example**

```http
GET https://api.collectorcrypt.com/marketplace?page=1&step=30&cardType=Card
```

**Response shape (shortened)**

```jsonc
{
  "findTotal": 53300,        // hits for this query
  "total":     69837,        // total cards in the category
  "totalPages": 1777,
  "cardsQtyByCategory": { "Pokemon": 45219, "One Piece": 4761, ... },
  "filterNFtCard": [
    {
      "id":            "2025101749C60884",
      "itemName":      "2000 #12 Dark Slowbro-Holo CGC 8.5 Rocket Pokemon",
      "category":      "Pokemon",
      "year":          2000,
      "set":           "Team Rocket - Unlimited - English",
      "gradingCompany":"CGC",
      "grade":         "NM/MINT+ 8.5",
      "gradeNum":      8.5,
      "insuredValue":  "51",
      "nftAddress":    "9ZeqMbsGJzZkphmMREKpooMR3jZDe97SGgdtdvQsBGeJ",
      "blockchain":    "Solana",
      "frontImage":    "https://arweave.net/...",
      "backImage":     "https://arweave.net/...",
      "images": {
        "front":  "https://d1xpxki1g4htqu.cloudfront.net/...",
        "frontM": "...",
        "frontS": "...",
        "back":   "...",
        "backM":  "...",
        "backS":  "..."
      },
      "listing": {
        "price":      150,
        "currency":   "USDC",
        "sellerId":   "cmleeoiaw0fb010o76hrvh7xm",
        "marketplace":"CC",
        "createdAt":  "2026-06-01T17:02:49.944",
        "updatedAt":  "2026-06-01T17:02:57.336",
        "receiptId":  "v2_514hm3kZDf8JSkti"
      },
      "offers": [{ "id": "239de17b-..." }],
      "owner":  { "id": "...", "wallet": "BJZJ..." }
    }
    // ...
  ]
}
```

### Detail pages (frontend route)

```
https://collectorcrypt.com/assets/solana/<nftAddress>
```

Pure frontend URL. The data source is the same API + RPC reads against Solana.

---

## Endpoint registries from the bundle

The following paths are **string literals** in the frontend. The method
(GET vs POST) is not directly visible there; it depends on the wrapper call.
See above for the snapshot date.

### Marketplace / Listings

| Path                                       | Purpose (assumed) |
|--------------------------------------------|-------------------|
| `marketplace`                              | public listings (see above) |
| `marketplace/cards`                        | frontend route (not an API) |
| `marketplace/broadcast`                    | broadcast TX after on-chain sign |
| `marketplace/list`                         | list a card |
| `marketplace/buy`                          | initiate purchase |
| `marketplace/buy/card`                     | start credit-card checkout |
| `marketplace/buy/card/pending`             | pending card payment status |
| `marketplace/cancel-listing`               | withdraw a listing |
| `marketplace/make-offer`                   | submit an offer |
| `marketplace/update-offer`                 | change an offer |
| `marketplace/accept-offer`                 | accept an offer |
| `marketplace/cancel-offer`                 | withdraw an offer |
| `marketplace/update-listing`               | change price etc. |
| `marketplace/cards/request-buyback-bulk`   | buy-back request (multiple cards) |
| `calcListingFee`                           | compute listing fee |
| `checkListingStatus`                       | status of an on-chain listing |
| `createAcceptOfferTx` / `…V2`              | build TX for accepting an offer |

### Account / Cards

| Path                                | Purpose |
|-------------------------------------|---------|
| `cards`                             | cards of the logged-in user (401 without auth) |
| `cards/{wallet}`                    | cards of a wallet |
| `cards/{wallet}/external`           | external NFTs of the wallet |
| `cards/export`                      | CSV export of your own cards |
| `cards/update`                      | update card metadata |
| `cards/publicNft`                   | public NFT card |
| `cards/refresh-card` / `refresh-cards` | reload metadata |
| `cards/shipping`                    | shipping info for cards |
| `cards/gemrate-options`             | grading options |
| `cards/p2p/send` / `p2p/approve`    | P2P transfer of a card |
| `account/{id}/activity`             | activity feed |
| `account/{id}/listings`             | account's listings |
| `account/{id}/offers-made`          | offers made |
| `account/{id}/offers-received`      | offers received |
| `account/{id}/cards`                | account's cards |
| `account/{id}/sealed`               | sealed products |
| `account/{id}/comics`               | comics |
| `account/{id}/comics-raw`           | raw comics |
| `account/{id}/merch`                | merch |
| `account/{id}/favorites`            | favorites |
| `account/{id}/following`            | followed accounts |

### Blockchain helpers

| Path                              | Purpose |
|-----------------------------------|---------|
| `blockchain/listing/{id}`         | re-sync on-chain listing |
| `blockchain/offers/{id}`          | re-sync on-chain offers |
| `blockchain/{chain}/burn/create`  | prepare burn TX |
| `blockchain/{chain}/burn`         | execute burn |
| `blockchain/{chain}/pay/card/confirm` | confirm card payment |
| `blockchain/checkout` / `…/estimate`  | build / estimate checkout TX |
| `blockchain/prepay`               | prepay flow |

### Buy / Pay

| Path                       | Purpose |
|----------------------------|---------|
| `buy/card/prepare`         | prepare card checkout |
| `buy/card/checkout`        | run card checkout |
| `buy/card/token-checkout`  | token-based checkout |
| `buy/card/confirm`         | confirm |
| `buy/card/cancel`          | cancel |
| `pay/card/confirm`         | confirm payment |
| `pay/card`                 | create payment entry |
| `buy/send`                 | trigger shipping |

### Grading

| Path                                                | Purpose |
|-----------------------------------------------------|---------|
| `grading/submissions`                               | list / create |
| `grading/submissions/{id}`                          | detail |
| `grading/submissions/{id}/cards`                    | add cards |
| `grading/submissions/{id}/cards/{cardId}`           | remove card |
| `grading/submissions/{id}/offer`                    | view offer |
| `grading/submissions/{id}/offer/accept`             | accept offer |
| `grading/submissions/{id}/outcome`                  | select outcome |
| `grading/submissions/{id}/submit`                   | submit |
| `grading/submissions/{id}/invoice`                  | invoice |
| `grading/submissions/partners`                      | partner list |
| `grading/submissions/photo-upload`                  | photo upload |

### Shipping / Redeem

| Path                                  | Purpose |
|---------------------------------------|---------|
| `shipping-address`                    | address list |
| `shipping-address/create` / `update`  | create / change address |
| `shipping/cancel`                     | cancel shipping |
| `shipping/status-filter`              | filter options |
| `shipping/{id}/upload-expected`       | upload expected cards |
| `shipping/{id}/vault-items`           | vault items for shipment |
| `outbound-shipment/{id}`              | outbound detail |
| `outbound-shipment/export`            | export |
| `redeem/prepare`                      | prepare redeem |
| `redeem/resume/{token}`               | resume |
| `redeem/estimate`                     | estimate cost |

### Hidden Offers / Follows / Blocks / Notifications

| Path                                  | Purpose |
|---------------------------------------|---------|
| `hidden-offers/{id}`                  | hide/show offer |
| `follows/{userId}/following`          | follow |
| `follows/{userId}/status`             | follow status |
| `blocks` / `blocks/{id}`              | block list |
| `blocks?page=&limit=`                 | paginated |
| `blocks/{id}/status`                  | block status |
| `notifications`                       | list |

### Auth (Privy)

| Path                                          | Purpose |
|-----------------------------------------------|---------|
| `auth/confirmEmail/{token}`                   | confirm email |
| `auth/privyHydrate`                           | session hydration |
| `auth/intercom-token`                         | Intercom token |
| `api/v1/users/me`                             | profile |
| `api/v1/users/me/accept_terms`                | accept terms |
| `api/v1/oauth/init` / `authenticate` / `link` / `unlink` / `transfer` | OAuth flow |
| `api/v1/passkeys/authenticate(/init)`         | passkey login |
| `api/v1/passwordless/authenticate`            | magic link |
| `api/v1/passwordless_sms/authenticate`        | SMS login |
| `api/v1/siwe/authenticate`                    | Sign-In-With-Ethereum |
| `api/v1/siws/authenticate`                    | Sign-In-With-Solana |
| `api/v1/farcaster/authenticate` (+ `v2`)      | Farcaster login |
| `api/v1/telegram/authenticate`                | Telegram login |
| `api/v1/guest/authenticate`                   | guest session |
| `api/v1/custom_jwt_account/authenticate` / `link` | custom JWT |
| `api/v1/recovery/oauth/init(/icloud)` / `authenticate` | recovery flow |
| `api/v1/plugins/moonpay_on_ramp/sign`         | Moonpay on-ramp sign |

### Misc

| Path                  | Purpose |
|-----------------------|---------|
| `contact`             | feedback form |
| `verify_nft_card`     | verify NFT card |
| `users/info`          | public user info |
| `users/invite`        | create invite |
| `users/invite-swap`   | invite swap |
| `users/update`        | update profile |
| `users/update/email`  | change email |
| `users/resetPassword` | reset password |
| `users/cookies`       | cookie settings |
| `all-users`           | user directory (admin?) |

---

## How to update

1. Read the current bundle URL from the HTML
   (`<script src="/main.<hash>.js">`).
2. Run the script:

   ```powershell
   python tools/discover_endpoints.py > endpoints.txt
   ```

3. Diff against the previous list, add new paths to the table above.
4. To confirm whether a path is public, try a `GET` with a `User-Agent`
   header (e.g. via `python -c "import requests; …"`).
   Response codes:
   - `200` → inspect the response
   - `400` → path exists, parameters missing/wrong
   - `401` → auth required
   - `404` → no GET (often POST-only) or path is wrong

---

## Trading client integration boundary (trader)

> **Status:** the authenticated trading client
> ([collectorcrypt/trader/ccapi.py](../collectorcrypt/trader/ccapi.py)) is built
> up to a *clean integration boundary*. It can authenticate, send and interpret
> the trading requests below, but it is **not** wired into live execution yet
> (ETAPPE 5) and the request/response shapes marked **ASSUMED** are
> reverse-engineered from the frontend bundle and **unverified**. Do not enable
> live spending until each flow is confirmed against a funded test wallet.

### Authentication

All trading endpoints require a Privy bearer token (see the *Auth (Privy)*
table). The trader obtains it through a `SessionProvider`
([collectorcrypt/trader/auth.py](../collectorcrypt/trader/auth.py)):

- `NullSessionProvider` (default) — owns no credentials, always refuses. With
  it in place no authenticated request can be sent.
- `StaticTokenProvider` — wraps a pre-obtained token (`TRADER_CC_TOKEN`) for
  integration testing of the transport.
- `PrivySiwsProvider` — the real Sign-In-With-Solana handshake (ETAPPE 4).

The token is sent as `Authorization: Bearer <token>`, held only in memory and
**redacted from all logs**. It is never written to the order store.

### Privy SIWS handshake (VERIFIED — end-to-end 2026-06-06)

> **Verified end-to-end** on 2026-06-06: a captured live `authenticate` request
> plus a full handshake from `PrivySiwsProvider` that minted a Privy bearer JWT,
> which the CollectorCrypt trading API then accepted. Key correction from the
> earlier probe: **SIWS does not run on the CC API** — it runs on **Privy's own
> API host** `https://auth.privy.io`. The CC API base (`api.collectorcrypt.com`)
> returns `404` for these paths.

**Confirmed environment** (extracted from the public frontend bundle — these are
client-side public values, not secrets):

| Value | Source |
|-------|--------|
| Privy auth host | `https://auth.privy.io` |
| `PRIVY_APP_ID` | `cmdgt21w400lgky0mkn069jui` |
| `PRIVY_CLIENT_ID` | `client-WY6NvtFJDWADQMppqbxv6hSrGa1igpPo8eVK9DfhnSGTi` |
| `NETWORK` | `mainnet` |

**Required headers** (Privy enforces a CORS-style check — without `Origin` it
returns `403 missing_origin`):

```
Content-Type: application/json
Origin: https://collectorcrypt.com
Referer: https://collectorcrypt.com/
privy-app-id: cmdgt21w400lgky0mkn069jui
privy-client-id: client-WY6NvtFJDWADQMppqbxv6hSrGa1igpPo8eVK9DfhnSGTi
privy-client: react-auth:3.28.0
privy-ca-id: <per-device uuid v4>
```

1. **init** — ✅ **VERIFIED** `POST https://auth.privy.io/api/v1/siws/init`
   with body `{ "address": "<wallet>" }`. Returns **HTTP 200**:

   ```jsonc
   { "nonce": "<64-char hex>", "address": "<wallet>",
     "expires_at": "2026-06-06T21:12:16.666Z" }   // nonce TTL ~10 min, no message
   ```

2. **sign** — the client builds the SIWS message locally (Privy returns no
   ready message) and signs it. ⚠️ **The signature is sent base64-encoded**
   (`Wallet.sign_message(..., encoding="base64")`), *not* base58 — this was the
   original cause of the authenticate `400`. **Exact Privy Solana template**:

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

3. **authenticate** — ✅ **VERIFIED** `POST .../api/v1/siws/authenticate`.
   Body (note `walletClientType` is **capitalised** `"Phantom"`, and there is
   **no** `address`/`nonce` field — the nonce travels inside the signed
   message):

   ```jsonc
   { "message": "<full SIWS message>",
     "signature": "<base64 signature>",
     "walletClientType": "Phantom",
     "connectorType": "solana_adapter",
     "mode": "login-or-sign-up",
     "message_type": "plain" }
   ```

   Returns **HTTP 200** with a bearer JWT and an `account_id` of the form
   `did:privy:<id>`. That JWT is accepted directly by the CC trading API.

**Live-readiness gate** (`siws.check_live_ready`): live trading requires *all*
of `TRADER_LIVE=true`, a signing wallet, a non-`none` auth provider, and a
session that can actually be established now. Any missing precondition raises
`CCAuthError` — the trader refuses to act rather than run unauthenticated.

#### Open / unverified (SIWS)

- The **exact token + expiry JSON keys** in the authenticate response were not
  captured directly; `_extract_token`/`_extract_expiry` read the common Privy
  shapes defensively (the live handshake produced a usable token + expiry).
- **CC trading API accepts the Privy bearer token directly** — ✅ VERIFIED: a
  Privy session JWT sent as `Authorization: Bearer <jwt>` is accepted by
  `api.collectorcrypt.com`. No CC-side exchange is required. **There is no CC
  `users/me`** — `/api/v1/users/me` is a *Privy* path (404 on the CC API);
  account identity comes from the wallet address.
- Refresh strategy: there is no separate refresh endpoint wired; the provider
  re-runs the full SIWS handshake on expiry (the wallet can always re-sign).

### Trading API transport (VERIFIED — probe 2026-06-06)

> Confirmed live with a real bearer token (read-only: the only write probed was
> `marketplace/buy`, which returns an **unsigned** transaction — it was never
> signed or broadcast, so nothing settled on-chain).

The CC API base is `https://api.collectorcrypt.com/`. The frontend uses three
call styles (extracted from the bundle's axios wrappers):

| Style | HTTP | URL | Body | Used by |
|-------|------|-----|------|---------|
| REST POST (`bE`) | POST | `<path>` | object | `marketplace/buy`, `marketplace/broadcast`, `marketplace/make-offer`, `marketplace/list`, `marketplace/cancel-listing`, `marketplace/cancel-offer`, `marketplace/accept-offer` |
| RPC `/v2` (`Qk`) | POST | `/v2` | `{method, params}` | `checkListingStatus` |
| RPC root (`kM`) | POST | `/` | `{method, params}` | `createQuickBuyTx`, `createMakeOfferTx`, `createCancelListingTx`, `getCardOffers` |

Auth: `Authorization: Bearer <privy-jwt>` plus `Origin: https://collectorcrypt.com`.

### Trading flows (VERIFIED — probe 2026-06-06)

1. **Buy (prepare)** — ✅ **VERIFIED HTTP 201**
   `POST https://api.collectorcrypt.com/marketplace/buy`:

   ```jsonc
   { "currency": "USDC", "nftAddress": "<nft>", "price": 900,
     "wallet": "<buyer wallet>", "fundingSource": "wallet" }
   ```

   Response is a **bare base64 `VersionedTransaction` string** (the response
   body *is* the base64 tx — there is no JSON envelope). The frontend does
   `VersionedTransaction.deserialize(Buffer.from(response.data, "base64"))`.
   `fundingSource` is `"wallet"` (direct) or `"escrow"`. **There is no
   `receiptId` in the buy body** (the old assumption was wrong); the required
   fields are `currency`, `nftAddress`, `price`, `wallet`.

2. **Listing status** — ✅ **VERIFIED HTTP 200**
   `POST https://api.collectorcrypt.com/v2`:

   ```jsonc
   { "method": "checkListingStatus",
     "params": { "nftAddress": "<nft>", "wallet": "<wallet>" } }
   ```

   `params` is **objectStrict**: exactly `nftAddress` + `wallet`, any other key
   (e.g. `receiptId`) is rejected. Response shape:
   `{ "exists": bool, "marketplace": str|null, "listing": object|null }`.

3. **Sign** — done locally by `Wallet.sign_transaction` (`solders`,
   base64 `VersionedTransaction`). No key ever leaves the process.

4. **Broadcast** — ✅ **VERIFIED HTTP 200** (DevTools capture 2026-06-07)
   `POST marketplace/broadcast`:

   ```jsonc
   { "signedTransaction": "<base64 signed tx>", "wallet": "<wallet>",
     "nftAddress": "<nft, optional>" }
   ```

   The old assumption (`{ signedTransaction }` only) was **missing `wallet`**.
   Response is JSON:

   ```jsonc
   { "success": true, "signature": "<sig>",
     "message": "Transaction broadcast successfully" }
   ```

   `_is_confirmed` reads the `success` flag; `_extract_signature` reads
   `signature`. This is the only step that finalises a trade, so it is
   **never retried**.

#### Offer lifecycle (VERIFIED — DevTools capture 2026-06-07)

These are the money-moving offer-penetration writes (Etappe 8.1). Each returns a
**bare base64 transaction** to sign + broadcast; a resting `OPEN` offer never
passes through `SIGNED`, so bump/cancel sign+broadcast **without** an order
transition (only make-offer uses the full SUBMITTED→…→OPEN flow).

5. **Make offer (prepare)** — ✅ **VERIFIED**
   `POST marketplace/make-offer`:

   ```jsonc
   { "cardId": "<raw CC card id, e.g. 2024122019C5785>", "currency": "USDC",
     "nftAddress": "<nft>", "price": 8, "wallet": "<bidder wallet>" }
   ```

   `cardId` is the card's internal CC id (the raw card top-level `id`) and is
   **required** — the old body `{ nftAddress, price, currency }` was rejected
   with **HTTP 400**. Response is a bare base64 tx.

6. **Update offer (bump)** — ✅ **VERIFIED**
   `POST marketplace/update-offer`:

   ```jsonc
   { "buyer": "<bidder wallet>", "currency": "USDC", "nftAddress": "<nft>",
     "price": 9, "wallet": "<bidder wallet>" }
   ```

   `buyer` and `wallet` are both the bidder's address. This is a real offer
   **edit** (one re-notification of the owner), answering the §9.2 open
   question — bumping is *not* cancel+remake. Response is a bare base64 tx.

7. **Cancel offer** — ✅ **VERIFIED**
   `POST marketplace/cancel-offer`:

   ```jsonc
   { "coin": "USDC", "keepInEscrow": false, "nftAddress": "<nft>",
     "wallet": "<bidder wallet>" }
   ```

   The currency field is **`coin`** (not `currency`) and there is **no offer
   id** — the offer is keyed by `nftAddress` + `wallet`. The old body
   `{ "id": <offer_id> }` was wrong. Response is a bare base64 tx.

#### Owned cards / sold signal (VERIFIED — DevTools capture 2026-06-07)

8. **Owned cards** — ✅ **VERIFIED HTTP 200**
   `GET cards/{wallet}/?page=1&step=96&orderBy=dateDesc`. Returns
   `{ totalCards, totalPages, filterNFtCard: [ ...cards ] }`. Each card carries
   `nftAddress`, `id`, `listing` (object | `null`), `listedAt`, `status`
   (the on-chain transfer status, e.g. `"Transferred"`) and **`oraclePrice`**
   (a per-card market value — the candidate source for the §9 market re-check).

   **Sold signal:** the endpoint lists **only cards still owned**. There is no
   per-card `"Sold"` status — a held card that has sold (or been transferred
   away) is simply **absent** from `filterNFtCard`. Absence from the fully-paged
   owned set is therefore the authoritative sold/exited signal, used by the
   engine's `ownership_sync` pass to mark a holding `sold`. Listed vs held is
   read from `listing`/`listedAt` (non-null = listed). A read, so it is
   idempotent and retryable; it fails safe (marks nothing sold) if the owned
   set cannot be fetched completely.

#### Open / unverified (trading)

- The **listing markdown** body (`marketplace/update-listing`) and **accept-offer**
  body (`marketplace/accept-offer`) — paths confirmed from the bundle map, bodies
  not yet probed (these LiveExecutor methods stay safe-failure no-ops).
- `getCardOffers` (RPC root) response shape — not probed.
- The `checkListingStatus` **status vocabulary** when a listing *is* active
  (probed listing returned `exists:false` for our wallet).

<!-- legacy assumed section retained below for history -->

### Assumed trading flows (SUPERSEDED by the verified section above)

The buy / offer / list flow is assumed to be a two-step **prepare → broadcast**:

1. **Prepare** — POST to `marketplace/buy` · `marketplace/make-offer` ·
   `marketplace/list`. Assumed request body:

   ```jsonc
   { "nftAddress": "<nft>", "price": 150, "currency": "USDC",
     "receiptId": "<listing receiptId, buy only>" }
   ```

   Assumed to return a serialized, **unsigned** Solana transaction for the
   wallet to sign locally.

2. **Sign** — done locally by `Wallet.keypair()` (`solders`). No key ever
   leaves the process; CC never sees the private key.

3. **Broadcast** — POST the signed transaction to `marketplace/broadcast`:

   ```jsonc
   { "signedTransaction": "<base64/base58 signed tx>" }
   ```

   Assumed to return the on-chain signature plus a receipt id. This is the only
   step that finalises a trade, so the client **never retries it automatically**
   — idempotency is enforced upstream via the persisted `client_order_id`.

### Error & retry policy

| Outcome      | Mapped error          | Retried automatically? |
|--------------|-----------------------|------------------------|
| 401 / 403    | `CCAuthError`         | no (session invalidated) |
| 429          | `CCRateLimitError`    | yes, honouring `Retry-After` (reads only) |
| 4xx          | `CCClientError`       | no |
| 5xx          | `CCServerError`       | yes, backoff (reads only) |
| network/timeout | `CCNetworkError`   | yes, backoff (reads only) |

State-changing trading calls (`buy`/`make-offer`/`list`/`broadcast`/`cancel-*`)
are **never** auto-retried — a silent retry could double-spend.

### Live executor flow (ETAPPE 5)

The `LiveExecutor`
([collectorcrypt/trader/executor.py](../collectorcrypt/trader/executor.py))
drives the assumed flow above per order, with no fire-and-forget. It is reached
**only** when the engine confirms live mode is fully armed
(`TRADER_LIVE=true` **and** a signing wallet **and** a non-`none` auth provider);
demo cycles are always dry-run.

Per-order state machine (persisted after every transition):

```
PLANNED ─preflight─▶ SUBMITTED ─sign─▶ SIGNED ─broadcast─▶ PENDING ─▶ CONFIRMED  (buy)
                                                                   └─▶ OPEN       (offer rests)
   └───────────────────────── any failure ─────────────────────────▶ FAILED
```

**Preflight before every send** (a failed check fails *that* order and the
batch continues; nothing is sent):

- **Duplicate guard** — if the store already holds the same `client_order_id`
  past `PLANNED`, the order is skipped (protects cycle replays / restarts).
- **Budget guard** — the order cost must fit the remaining per-cycle budget
  envelope; each confirmed buy / opened offer decrements it.
- **Price/market sanity** — refuses to trade with no market reference, or at/
  above market value (the thesis is buying below market).

**Sign + broadcast** — the unsigned transaction returned by prepare is signed by
`Wallet.sign_transaction` (base64-decode → `solders` `VersionedTransaction`,
legacy fallback → re-encode base64) and broadcast. A broadcast accepted but not
yet on-chain stays `PENDING` for the reconciler — a successful send, not a
failure. Any `CCApiError`/`WalletError` marks the order `FAILED` (never retried).

**Relisting** — a confirmed buy with a positive resale price creates its linked
`LIST` order as a **`PLANNED` relist candidate** and persists it; the live
exit/relisting flow itself is **ETAPPE 6**, so nothing is listed on-chain yet.

#### Open / unverified (live executor)

- The exact response keys for the unsigned transaction (`transaction` / `tx` /
  `serializedTransaction` … are accepted defensively), the receipt/offer/listing
  id, the on-chain signature, and the confirmation/fill status.
- The transaction wire encoding (base64 assumed) and version (v0 assumed, legacy
  fallback) — `Wallet.sign_transaction` raises clearly if it cannot parse.
- Whether the wallet is the sole required signer at the prepare stage (assumed),
  or whether the marketplace co-signs and partial signatures must be preserved.
- Whether an accepted offer reports settlement on `broadcast` or only via a
  later `checkListingStatus` / status sync.

### Live exit / relisting + status sync (ETAPPE 6)

Two maintenance steps run at the end of every **live** cycle (never in
dry-run/demo), after new buys/offers are placed:

1. **Status sync** (`reconcile.StatusSyncer`) — authoritative reconciliation.
   For each in-flight order (`SUBMITTED`/`SIGNED`/`PENDING`/`OPEN`) it asks CC
   for the real status and transitions **only on clear evidence**:
   - reported confirmed → `CONFIRMED` (a confirmed buy with a resale price
     spawns its linked `PLANNED` relist candidate, idempotently);
   - reported accepted/filled (offer) → `CONFIRMED`;
   - reported cancelled/withdrawn/expired → `CANCELLED`.
   An order with no external id, an unreadable status, or an ambiguous response
   is left untouched and counted *unresolved*. A read error never transitions
   an order. Uses the client's safe, retryable reads (`me`,
   `checkListingStatus`).

2. **Exit / relisting** (`LiveExecutor.relist`) — loads the persisted relist
   candidates (`store.relist_candidates()`) and drives each onto the market via
   `marketplace/list` → sign → broadcast, with the same per-order state machine
   (`PLANNED → SUBMITTED → SIGNED → PENDING → CONFIRMED`). Sell-side preflight:
   a positive resale price and a duplicate-listing guard (no budget check —
   listing an owned card does not spend USDC). The relist price is the planned
   `market × (1 − TRADER_RESELL_DISCOUNT_PCT/100)`.

Running the sync *before* the exit pass means a buy confirmed *this* cycle (only
discovered via the sync, not at broadcast time) still spawns a relist candidate
that the exit pass lists in the same cycle.

The cycle report gains `status_sync` (the `StatusSyncReport`: counts of
`checked`/`confirmed`/`cancelled`/`relisted_spawned`/`unresolved`/`errors` plus
`transitions`) and `relisted` (per-listing summaries).

#### Open / unverified (exit + sync)

- The exact status-probe endpoint and payload per order kind (currently
  `checkListingStatus` is used as the general probe, keyed by `external_id`).
- The authoritative status vocabulary (the matcher accepts a broad set of
  confirmed/filled/cancelled synonyms defensively).
- Whether buys/offers expose a distinct status endpoint separate from listings.
- Whether listing requires a prior `calcListingFee` call or extra parameters
  (royalty, expiry) in the `marketplace/list` body.

### Holdings maintenance passes (holdings-lifecycle plan E6)

At the end of every **live** cycle (after the status sync + exit pass, never in
dry-run/demo, and skipped while the kill switch is tripped — except the
read-only re-check) the engine runs the offer-penetration and floor passes over
the persisted holdings/orders. Each pass selects its candidates with the pure
logic in [collectorcrypt/trader/holdings.py](../collectorcrypt/trader/holdings.py)
and then asks the executor to act:

- **Offer bump** — aged open offers (`should_bump`) are re-priced up by
  `TRADER_OFFER_BUMP_USD` to re-trigger the owner notification.
- **Offer cancel** — offers that exhausted their bumps (`should_cancel_offer`)
  are withdrawn so escrow refunds.
- **Listing markdown** — listed-but-unsold cards (`is_due_for_markdown`) step
  toward the cost-basis floor.
- **Offer accept** — cards parked at the floor long enough
  (`is_due_for_offer_accept`) become eligible to accept the best incoming bid.
- **Market re-check** — a read-only inventory stub (runs even while halted); the
  per-NFT market-value *source* is an open decision (plan §9) and not wired yet.

The cycle report gains `bumped`, `cancelled`, `marked_down`, `offers_accepted`
and `market_recheck`.

> **Safe-failure (no money moved).** On the `LiveExecutor` all four
> state-changing maintenance actions (`bump_offer`, `cancel_offer`,
> `markdown_listing`, `accept_offer`) are **no-ops** that leave the order
> untouched and call nothing — their detail reads
> `… skipped: live shape not verified (ETAPPE 8)`. The `DryRunExecutor`
> simulates the transitions for the report. The passes therefore surface
> *which* offers/holdings are due without sending anything until Etappe 8
> verifies the shapes against a funded wallet.

The supporting client methods are **ASSUMED / unverified** seams in
[collectorcrypt/trader/ccapi.py](../collectorcrypt/trader/ccapi.py):

| Method | Endpoint | Kind | Body (assumed) | Notes |
|--------|----------|------|----------------|-------|
| `get_card_offers(nft)` | RPC `getCardOffers` | read (retryable) | `{nftAddress}` | list incoming bids for a held card |
| `update_listing(nft, price)` | `marketplace/update-listing` | write (non-retryable) | `{nftAddress, price, currency}` | re-price a live listing (markdown / bump-equivalent) |
| `accept_offer(offer_id, nft)` | `marketplace/accept-offer` | write (non-retryable) | `{id, nftAddress?}` | accept a specific incoming bid |

Reads are idempotent and retry on 429/5xx/network; the two writes are never
auto-retried (no double-accept / double-update).

### Risk engine / limits (ETAPPE 7)

The risk engine
([collectorcrypt/trader/risk.py](../collectorcrypt/trader/risk.py)) is the final
gate before any live order is sent — independent of the planner, so a planning
bug or market anomaly cannot drain the wallet. On every **live** cycle the
engine evaluates the planned orders against operator-set limits *after*
planning and *before* the executor runs. Blocked orders are transitioned to
`FAILED` with a `risk gate: …` detail and **never reach the executor**;
allowed orders proceed normally. Dry-run/demo cycles are not gated, but the
posture is still computed for display.

Enforced controls (each limit is `0` = disabled, so existing setups are
unchanged until an operator opts in):

| Limit env var                      | Control |
|------------------------------------|---------|
| `TRADER_MAX_CONSECUTIVE_FAILURES`  | Kill switch — after N real orders fail in a row, **halt all** trading this cycle (and skip the exit/relist pass; the read-only status sync still runs). |
| `TRADER_MAX_OPEN_POSITIONS`        | Cap on concurrent real in-flight orders. |
| `TRADER_MAX_SPEND_PER_CYCLE_USD`   | Ceiling on USD committed in one cycle. |
| `TRADER_MAX_SPEND_PER_DAY_USD`     | Rolling 24h ceiling on realized USD spend across cycles. |

Usage is read from the durable store: `open_position_count()` (real active
orders), `confirmed_spend_since(ts)` (sum of `price_usd` of confirmed,
non-simulated buys/offers in the last 24h) and `recent_terminal_statuses()`
(for the consecutive-failure streak). Only spending orders (`buy`/`offer`)
count against the caps; relists (sells) never do.

**Fail-safe:** any failure to read the risk state resolves to *halt* — zero
orders sent. `RiskEngine.evaluate` never raises; the caller simply respects
`decision.allowed`.

The cycle report gains `risk` (the posture: `enabled`, `halted`, `limits`,
`usage`, `cycle.{planned_spend,allowed,blocked}`, `breaches`). The manager
snapshot also exposes a read-only `risk` posture (no pending orders) so the
dashboard shows the caps, today's spend, open positions and kill-switch state
even before a cycle runs.

#### Open / unverified (risk)

- "Spend" counts a confirmed offer's full `price_usd` as realized; whether an
  accepted offer settles for exactly the bid is assumed.
- The daily window is a simple rolling 24h on `created_at`; it does not align
  to a wallet/exchange settlement day.

### Crash recovery / auto-resume (ETAPPE 8)

The durable store already preserves every order and its lifecycle across a
restart, but the **loop control state** (active / paused / interval) used to
live only in memory — so after a crash the bot sat idle until an operator
clicked *Start loop*. The manager
([collectorcrypt/trader/manager.py](../collectorcrypt/trader/manager.py)) now
persists that state and can opt-in resume it.

* **Persisted loop state.** Every loop control change (`start_loop`, `pause`,
  `resume`, `stop`) writes `{loop_active, paused, interval}` to a small
  `runtime` key/value table in the store (`set_runtime`/`get_runtime`). It
  holds **no secrets** — only public control flags.
* **Startup reconcile.** On construction the manager runs a single read-only
  reconciliation so the UI immediately reflects any orders that were in flight
  when the process stopped. This never submits, signs or cancels anything; the
  authoritative `StatusSyncer` resolves them on the next live cycle.
* **Opt-in auto-resume.** Only when `TRADER_AUTO_RESUME=true` **and** the loop
  was active before the restart is the worker restarted — in exactly the mode
  that is configured now. Like `TRADER_LIVE`, the flag is read **from the
  environment only** (never from the UI overrides file), so a crash can never
  silently arm trading. The live/auth gates are unchanged: auto-resume only
  continues the loop in the already-configured mode; it does not enable live
  trading by itself.

The manager snapshot gains a `recovery` block:

| Field          | Meaning |
|----------------|---------|
| `performed`    | Recovery ran at startup. |
| `in_flight`    | Active orders found by the startup reconcile. |
| `was_active`   | The loop was active in the persisted state. |
| `auto_resume`  | `TRADER_AUTO_RESUME` is set. |
| `resumed`      | The loop was actually restarted (needs both of the above). |

**Fail-safe:** persistence and recovery are best-effort and never block the
control flow or startup — a store error leaves the in-memory state
authoritative and defaults auto-resume to *off*.

#### Open / unverified (recovery)

- Auto-resume restores the loop in the persisted mode (e.g. paused stays
  paused); it does not retroactively run cycles missed while the process was
  down.
- A still-running second instance pointing at the same store could both resume;
  single-instance operation is assumed.

### Open / unverified

- Exact field names and casing of every POST body (camelCase assumed).
- Whether `price` is USD, USDC base units, or lamports for SOL listings.
- The transaction encoding expected by `broadcast` (base64 vs base58).
- Whether offers/listings return their own id immediately or only after a
  follow-up `checkListingStatus` / `blockchain/listing/{id}` sync.
- Rate-limit headers actually emitted by the API (we honour `Retry-After` if
  present, otherwise fall back to the shared backoff schedule).

