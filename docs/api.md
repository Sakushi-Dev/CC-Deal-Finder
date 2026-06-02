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
