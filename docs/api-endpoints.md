# CollectorCrypt API — Endpoint Registry

← [Index](index.md)

> String literals extracted from the frontend bundle (snapshot 2026-06-01).
> HTTP method is not directly visible in the bundle — see [api-trading.md](api-trading.md)
> for confirmed shapes. Regenerate with [tools/discover_endpoints.py](../tools/discover_endpoints.py).

---

## Marketplace / Listings

| Path | Purpose |
|------|---------|
| `marketplace` | public listings (see [api-overview.md](api-overview.md)) |
| `marketplace/broadcast` | broadcast signed TX after on-chain sign |
| `marketplace/list` | list a card |
| `marketplace/buy` | initiate purchase |
| `marketplace/buy/card` | start credit-card checkout |
| `marketplace/buy/card/pending` | pending card payment status |
| `marketplace/cancel-listing` | withdraw a listing |
| `marketplace/make-offer` | submit an offer |
| `marketplace/update-offer` | change an offer |
| `marketplace/accept-offer` | accept an offer |
| `marketplace/cancel-offer` | withdraw an offer |
| `marketplace/update-listing` | change price etc. |
| `marketplace/cards/request-buyback-bulk` | buy-back request (multiple cards) |
| `calcListingFee` | compute listing fee |
| `checkListingStatus` | status of an on-chain listing |
| `createAcceptOfferTx` / `…V2` | build TX for accepting an offer |

Trading client: [collectorcrypt/trader/ccapi.py](../collectorcrypt/trader/ccapi.py)

---

## Account / Cards

| Path | Purpose |
|------|---------|
| `cards` | cards of the logged-in user (401 without auth) |
| `cards/{wallet}` | cards of a wallet |
| `cards/{wallet}/external` | external NFTs of the wallet |
| `cards/export` | CSV export of your own cards |
| `cards/update` | update card metadata |
| `cards/publicNft` | public NFT card |
| `cards/refresh-card` / `refresh-cards` | reload metadata |
| `cards/shipping` | shipping info for cards |
| `cards/gemrate-options` | grading options |
| `cards/p2p/send` / `p2p/approve` | P2P transfer of a card |
| `card-activity/{nft}` | per-NFT activity feed (offers, cancels, accepts, listing edits) |
| `account/{id}/activity` | activity feed |
| `account/{id}/listings` | account's listings |
| `account/{id}/offers-made` | offers made |
| `account/{id}/offers-received` | offers received |
| `account/{id}/cards` | account's cards |
| `account/{id}/sealed` | sealed products |
| `account/{id}/comics` | comics |
| `account/{id}/comics-raw` | raw comics |
| `account/{id}/merch` | merch |
| `account/{id}/favorites` | favorites |
| `account/{id}/following` | followed accounts |

---

## Blockchain Helpers

| Path | Purpose |
|------|---------|
| `blockchain/listing/{id}` | re-sync on-chain listing |
| `blockchain/offers/{id}` | re-sync on-chain offers |
| `blockchain/{chain}/burn/create` | prepare burn TX |
| `blockchain/{chain}/burn` | execute burn |
| `blockchain/{chain}/pay/card/confirm` | confirm card payment |
| `blockchain/checkout` / `…/estimate` | build / estimate checkout TX |
| `blockchain/prepay` | prepay flow |

---

## Buy / Pay

| Path | Purpose |
|------|---------|
| `buy/card/prepare` | prepare card checkout |
| `buy/card/checkout` | run card checkout |
| `buy/card/token-checkout` | token-based checkout |
| `buy/card/confirm` | confirm |
| `buy/card/cancel` | cancel |
| `pay/card/confirm` | confirm payment |
| `pay/card` | create payment entry |
| `buy/send` | trigger shipping |

---

## Grading

| Path | Purpose |
|------|---------|
| `grading/submissions` | list / create |
| `grading/submissions/{id}` | detail |
| `grading/submissions/{id}/cards` | add cards |
| `grading/submissions/{id}/cards/{cardId}` | remove card |
| `grading/submissions/{id}/offer` | view offer |
| `grading/submissions/{id}/offer/accept` | accept offer |
| `grading/submissions/{id}/outcome` | select outcome |
| `grading/submissions/{id}/submit` | submit |
| `grading/submissions/{id}/invoice` | invoice |
| `grading/submissions/partners` | partner list |
| `grading/submissions/photo-upload` | photo upload |

---

## Shipping / Redeem

| Path | Purpose |
|------|---------|
| `shipping-address` | address list |
| `shipping-address/create` / `update` | create / change address |
| `shipping/cancel` | cancel shipping |
| `shipping/status-filter` | filter options |
| `shipping/{id}/upload-expected` | upload expected cards |
| `shipping/{id}/vault-items` | vault items for shipment |
| `outbound-shipment/{id}` | outbound detail |
| `outbound-shipment/export` | export |
| `redeem/prepare` | prepare redeem |
| `redeem/resume/{token}` | resume |
| `redeem/estimate` | estimate cost |

---

## Follows / Blocks / Notifications

| Path | Purpose |
|------|---------|
| `hidden-offers/{id}` | hide/show offer |
| `follows/{userId}/following` | follow |
| `follows/{userId}/status` | follow status |
| `blocks` / `blocks/{id}` | block list |
| `blocks?page=&limit=` | paginated |
| `blocks/{id}/status` | block status |
| `notifications` | list |

---

## Auth (Privy)

> SIWS is hosted on `https://auth.privy.io`, **not** on `api.collectorcrypt.com`.
> See [api-auth.md](api-auth.md) for the verified handshake.

| Path | Purpose |
|------|---------|
| `auth/confirmEmail/{token}` | confirm email |
| `auth/privyHydrate` | session hydration |
| `auth/intercom-token` | Intercom token |
| `api/v1/users/me` | Privy profile (404 on CC API) |
| `api/v1/users/me/accept_terms` | accept terms |
| `api/v1/oauth/init` / `authenticate` / `link` / `unlink` / `transfer` | OAuth flow |
| `api/v1/passkeys/authenticate(/init)` | passkey login |
| `api/v1/passwordless/authenticate` | magic link |
| `api/v1/passwordless_sms/authenticate` | SMS login |
| `api/v1/siwe/authenticate` | Sign-In-With-Ethereum |
| `api/v1/siws/authenticate` | Sign-In-With-Solana |
| `api/v1/farcaster/authenticate` (+ `v2`) | Farcaster login |
| `api/v1/telegram/authenticate` | Telegram login |
| `api/v1/guest/authenticate` | guest session |
| `api/v1/custom_jwt_account/authenticate` / `link` | custom JWT |
| `api/v1/recovery/oauth/init(/icloud)` / `authenticate` | recovery flow |
| `api/v1/plugins/moonpay_on_ramp/sign` | Moonpay on-ramp sign |

---

## Misc

| Path | Purpose |
|------|---------|
| `contact` | feedback form |
| `verify_nft_card` | verify NFT card |
| `users/info` | public user info |
| `users/invite` | create invite |
| `users/invite-swap` | invite swap |
| `users/update` | update profile |
| `users/update/email` | change email |
| `users/resetPassword` | reset password |
| `users/cookies` | cookie settings |
| `all-users` | user directory (admin?) |

---

## RPC styles

The frontend uses three call patterns (confirmed via bundle analysis):

| Style | HTTP | URL | Body | Used by |
|-------|------|-----|------|---------|
| REST POST (`bE`) | POST | `<path>` | object | `marketplace/buy`, `broadcast`, `make-offer`, `list`, `cancel-*`, `accept-offer` |
| RPC `/v2` (`Qk`) | POST | `/v2` | `{method, params}` | `checkListingStatus` |
| RPC root (`kM`) | POST | `/` | `{method, params}` | `createQuickBuyTx`, `createMakeOfferTx`, `createCancelListingTx`, `getCardOffers` |
