# CollectorCrypt – inoffizielle API-Notizen

> **Stand:** 2026-06-01 · Bundle `main.97af84c3de44d9b7884c.js`
>
> Diese Dokumentation ist **rein reverse-engineered** aus dem öffentlichen
> Frontend-Bundle und ist nicht offiziell. Endpoints können sich jederzeit
> ändern. Mit [tools/discover_endpoints.py](../tools/discover_endpoints.py)
> kann die Liste neu generiert werden.

## Basis

- **Base URL:** `https://api.collectorcrypt.com`
- **Format:** JSON
- **Auth:** ein Teil der Endpoints ist öffentlich (Marketplace-Listings),
  andere brauchen ein Bearer-/Cookie-Token aus dem Privy-Login
  (`/api/v1/...authenticate`). Ohne Token → `401 Unauthorized`.
- **Frontend ↔ API:** Der React-SPA-Client kapselt alle Aufrufe in kleinen
  Wrapper-Funktionen (`(0,x.Jt)(path)` = GET, `(0,x.bE)(path,body)` = POST/PUT).
  Pfade aus dem Bundle werden relativ zur Base-URL aufgelöst.

---

## Bestätigte öffentliche Endpoints

### `GET /marketplace`

Liefert paginierte Listings für eine Kategorie. Diese App benutzt diesen
Endpoint.

**Query-Parameter**

| Name                  | Pflicht | Beispiel        | Beschreibung |
|-----------------------|---------|-----------------|--------------|
| `cardType`            | ja      | `Card`          | einer von `Card`, `Comic`, `ComicRaw`, `Game`, `Merch`, `Raw`, `Sealed` |
| `page`                | ja      | `1`             | 1-basierter Seitenindex |
| `step`                | ja      | `30`            | Karten pro Seite (im UI bis 100) |
| `search`              | nein    | `charizard`     | Volltextsuche; `+`, `&`, `#` müssen URL-encoded werden |
| `autographed`         | nein    | `true`          | nur signierte Karten |
| `authenticated`       | nein    | `true`          | nur authentifizierte Karten |
| `marketplaceStatus`   | nein    | `Listed,Sold`   | Komma-Liste; nur diese Listing-Stati |
| `marketplaceTags`     | nein    | `Promo`         | Komma-Liste Tags |
| `insuredValueMin`     | nein    | `100`           | Mindest-Versicherungswert (USD) |
| `insuredValueMax`     | nein    | `1000`          | Max-Versicherungswert (USD) |

**Beispiel**

```http
GET https://api.collectorcrypt.com/marketplace?page=1&step=30&cardType=Card
```

**Antwortform (gekürzt)**

```jsonc
{
  "findTotal": 53300,        // Treffer in dieser Abfrage
  "total":     69837,        // Karten der Kategorie insgesamt
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

### Detailseiten (Frontend-Route)

```
https://collectorcrypt.com/assets/solana/<nftAddress>
```

Reine Frontend-URL. Datenquelle ist dieselbe API + RPC-Reads gegen Solana.

---

## Endpoint-Registries aus dem Bundle

Folgende Pfade sind **als String-Literale** im Frontend hinterlegt. Methode
(GET vs POST) ist dort nicht direkt sichtbar; sie wird über die jeweiligen
Wrapper-Aufrufe bestimmt. Stand siehe oben.

### Marketplace / Listings

| Pfad                                       | Zweck (vermutet) |
|--------------------------------------------|------------------|
| `marketplace`                              | öffentliche Listings (s.o.) |
| `marketplace/cards`                        | Frontend-Route (kein API) |
| `marketplace/broadcast`                    | TX nach Sign-On-Chain broadcasten |
| `marketplace/list`                         | Karte listen |
| `marketplace/buy`                          | Kauf einleiten |
| `marketplace/buy/card`                     | Kreditkarten-Checkout starten |
| `marketplace/buy/card/pending`             | Status pending-Card-Payment |
| `marketplace/cancel-listing`               | Listing zurückziehen |
| `marketplace/make-offer`                   | Offer abgeben |
| `marketplace/update-offer`                 | Offer ändern |
| `marketplace/accept-offer`                 | Offer annehmen |
| `marketplace/cancel-offer`                 | Offer zurückziehen |
| `marketplace/update-listing`               | Preis o.ä. ändern |
| `marketplace/cards/request-buyback-bulk`   | Buy-Back-Anfrage (mehrere Karten) |
| `calcListingFee`                           | Listing-Gebühr berechnen |
| `checkListingStatus`                       | Status eines On-Chain-Listings |
| `createAcceptOfferTx` / `…V2`              | TX zum Annehmen einer Offer bauen |

### Konto / Karten

| Pfad                                | Zweck |
|-------------------------------------|-------|
| `cards`                             | Karten des eingeloggten Users (401 ohne Auth) |
| `cards/{wallet}`                    | Karten eines Wallets |
| `cards/{wallet}/external`           | externe NFTs des Wallets |
| `cards/export`                      | CSV-Export der eigenen Karten |
| `cards/update`                      | Karten-Metadaten aktualisieren |
| `cards/publicNft`                   | öffentliche NFT-Karte |
| `cards/refresh-card` / `refresh-cards` | Metadaten neu laden |
| `cards/shipping`                    | Versandinfos für Karten |
| `cards/gemrate-options`             | Grading-Optionen |
| `cards/p2p/send` / `p2p/approve`    | P2P-Transfer einer Karte |
| `account/{id}/activity`             | Activity-Feed |
| `account/{id}/listings`             | Listings des Accounts |
| `account/{id}/offers-made`          | abgegebene Offers |
| `account/{id}/offers-received`      | erhaltene Offers |
| `account/{id}/cards`                | Karten des Accounts |
| `account/{id}/sealed`               | Sealed-Produkte |
| `account/{id}/comics`               | Comics |
| `account/{id}/comics-raw`           | Raw-Comics |
| `account/{id}/merch`                | Merch |
| `account/{id}/favorites`            | Favoriten |
| `account/{id}/following`            | gefolgte Accounts |

### Blockchain-Helfer

| Pfad                              | Zweck |
|-----------------------------------|-------|
| `blockchain/listing/{id}`         | On-Chain-Listing nachsynchronisieren |
| `blockchain/offers/{id}`          | On-Chain-Offers nachsynchronisieren |
| `blockchain/{chain}/burn/create`  | Burn-TX vorbereiten |
| `blockchain/{chain}/burn`         | Burn ausführen |
| `blockchain/{chain}/pay/card/confirm` | Card-Payment bestätigen |
| `blockchain/checkout` / `…/estimate`  | Checkout-TX bauen / schätzen |
| `blockchain/prepay`               | Prepay-Flow |

### Buy / Pay

| Pfad                       | Zweck |
|----------------------------|-------|
| `buy/card/prepare`         | Card-Checkout vorbereiten |
| `buy/card/checkout`        | Card-Checkout durchführen |
| `buy/card/token-checkout`  | Token-basierter Checkout |
| `buy/card/confirm`         | Bestätigen |
| `buy/card/cancel`          | Abbrechen |
| `pay/card/confirm`         | Zahlung bestätigen |
| `pay/card`                 | Zahlungs-Eintrag erzeugen |
| `buy/send`                 | Versand auslösen |

### Grading

| Pfad                                                | Zweck |
|-----------------------------------------------------|-------|
| `grading/submissions`                               | Liste / Erstellen |
| `grading/submissions/{id}`                          | Detail |
| `grading/submissions/{id}/cards`                    | Karten hinzufügen |
| `grading/submissions/{id}/cards/{cardId}`           | Karte entfernen |
| `grading/submissions/{id}/offer`                    | Offer einsehen |
| `grading/submissions/{id}/offer/accept`             | Offer annehmen |
| `grading/submissions/{id}/outcome`                  | Outcome wählen |
| `grading/submissions/{id}/submit`                   | absenden |
| `grading/submissions/{id}/invoice`                  | Rechnung |
| `grading/submissions/partners`                      | Partner-Liste |
| `grading/submissions/photo-upload`                  | Foto-Upload |

### Shipping / Redeem

| Pfad                                  | Zweck |
|---------------------------------------|-------|
| `shipping-address`                    | Adressliste |
| `shipping-address/create` / `update`  | Adresse anlegen / ändern |
| `shipping/cancel`                     | Shipping abbrechen |
| `shipping/status-filter`              | Filter-Optionen |
| `shipping/{id}/upload-expected`       | erwartete Karten upload |
| `shipping/{id}/vault-items`           | Vault-Items zur Sendung |
| `outbound-shipment/{id}`              | Outbound-Detail |
| `outbound-shipment/export`            | Export |
| `redeem/prepare`                      | Redeem vorbereiten |
| `redeem/resume/{token}`               | fortsetzen |
| `redeem/estimate`                     | Kosten schätzen |

### Hidden Offers / Follows / Blocks / Notifications

| Pfad                                  | Zweck |
|---------------------------------------|-------|
| `hidden-offers/{id}`                  | Offer ein-/ausblenden |
| `follows/{userId}/following`          | folgen |
| `follows/{userId}/status`             | Follow-Status |
| `blocks` / `blocks/{id}`              | Blockliste |
| `blocks?page=&limit=`                 | paginiert |
| `blocks/{id}/status`                  | Block-Status |
| `notifications`                       | Liste |

### Auth (Privy)

| Pfad                                          | Zweck |
|-----------------------------------------------|-------|
| `auth/confirmEmail/{token}`                   | E-Mail bestätigen |
| `auth/privyHydrate`                           | Sessionhydration |
| `auth/intercom-token`                         | Intercom-Token |
| `api/v1/users/me`                             | Profil |
| `api/v1/users/me/accept_terms`                | AGB akzeptieren |
| `api/v1/oauth/init` / `authenticate` / `link` / `unlink` / `transfer` | OAuth-Flow |
| `api/v1/passkeys/authenticate(/init)`         | Passkey-Login |
| `api/v1/passwordless/authenticate`            | Magic-Link |
| `api/v1/passwordless_sms/authenticate`        | SMS-Login |
| `api/v1/siwe/authenticate`                    | Sign-In-With-Ethereum |
| `api/v1/siws/authenticate`                    | Sign-In-With-Solana |
| `api/v1/farcaster/authenticate` (+ `v2`)      | Farcaster-Login |
| `api/v1/telegram/authenticate`                | Telegram-Login |
| `api/v1/guest/authenticate`                   | Gast-Session |
| `api/v1/custom_jwt_account/authenticate` / `link` | Custom-JWT |
| `api/v1/recovery/oauth/init(/icloud)` / `authenticate` | Recovery-Flow |
| `api/v1/plugins/moonpay_on_ramp/sign`         | Moonpay On-Ramp Sign |

### Sonstiges

| Pfad                  | Zweck |
|-----------------------|-------|
| `contact`             | Feedback-Form |
| `verify_nft_card`     | NFT-Karte verifizieren |
| `users/info`          | öffentliche Userinfos |
| `users/invite`        | Invite erstellen |
| `users/invite-swap`   | Invite swap |
| `users/update`        | Profil ändern |
| `users/update/email`  | E-Mail ändern |
| `users/resetPassword` | Passwort zurücksetzen |
| `users/cookies`       | Cookie-Settings |
| `all-users`           | Userverzeichnis (Admin?) |

---

## Wie aktualisieren?

1. Aktuelle Bundle-URL aus dem HTML lesen
   (`<script src="/main.<hash>.js">`).
2. Skript ausführen:

   ```powershell
   python tools/discover_endpoints.py > endpoints.txt
   ```

3. Diff mit dem letzten Stand prüfen, neue Pfade hier in der Tabelle ergänzen.
4. Zum Bestätigen, dass ein Pfad öffentlich ist, einen `GET` mit
   `User-Agent`-Header probieren (z.B. via `python -c "import requests; …"`).
   Antwortcodes:
   - `200` → Antwort prüfen
   - `400` → Pfad existiert, Parameter fehlen/falsch
   - `401` → Auth nötig
   - `404` → kein GET (oft POST-only) oder Pfad falsch
