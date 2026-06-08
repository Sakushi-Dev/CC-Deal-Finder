# CollectorCrypt API — Overview & Public Endpoints

← [Index](index.md)

> **As of:** 2026-06-01 · Bundle `main.97af84c3de44d9b7884c.js`
>
> Purely reverse-engineered. Not official. Endpoints can change at any time.

---

## Basics

- **Base URL:** `https://api.collectorcrypt.com`
- **Format:** JSON
- **Auth:** Some endpoints are public (marketplace listings); others require a
  bearer/cookie token from the Privy login (`/api/v1/...authenticate`). Without
  a token → `401 Unauthorized`.
- **Frontend ↔ API:** The React SPA wraps calls in axios helpers (`(0,x.Jt)(path)` = GET,
  `(0,x.bE)(path,body)` = POST/PUT). Paths are resolved relative to the base URL.

Relevant source: [collectorcrypt/api.py](../collectorcrypt/api.py),
[collectorcrypt/config.py](../collectorcrypt/config.py)

---

## Confirmed public endpoints

### `GET /marketplace`

Returns paginated listings for a category.
Used by: [collectorcrypt/api.py](../collectorcrypt/api.py), [collectorcrypt/scanner.py](../collectorcrypt/scanner.py)

**Query parameters**

| Name                | Required | Example       | Description |
|---------------------|----------|---------------|-------------|
| `cardType`          | yes      | `Card`        | one of `Card`, `Comic`, `ComicRaw`, `Game`, `Merch`, `Raw`, `Sealed` |
| `page`              | yes      | `1`           | 1-based page index |
| `step`              | yes      | `30`          | cards per page (up to 100 in the UI) |
| `search`            | no       | `charizard`   | full-text search; `+`, `&`, `#` must be URL-encoded |
| `autographed`       | no       | `true`        | autographed cards only |
| `authenticated`     | no       | `true`        | authenticated cards only |
| `marketplaceStatus` | no       | `Listed,Sold` | comma list of listing statuses |
| `marketplaceTags`   | no       | `Promo`       | comma list of tags |
| `insuredValueMin`   | no       | `100`         | minimum insured value (USD) |
| `insuredValueMax`   | no       | `1000`        | maximum insured value (USD) |

**Example**

```http
GET https://api.collectorcrypt.com/marketplace?page=1&step=30&cardType=Card
```

**Response shape (shortened)**

```jsonc
{
  "findTotal": 53300,
  "total":     69837,
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
      "images": {
        "front": "https://d1xpxki1g4htqu.cloudfront.net/...",
        "frontM": "...", "frontS": "...",
        "back": "...", "backM": "...", "backS": "..."
      },
      "listing": {
        "price":      150,
        "currency":   "USDC",
        "sellerId":   "cmleeoiaw0fb010o76hrvh7xm",
        "marketplace":"CC",
        "createdAt":  "2026-06-01T17:02:49.944",
        "receiptId":  "v2_514hm3kZDf8JSkti"
      },
      "offers": [{ "id": "239de17b-..." }],
      "owner":  { "id": "...", "wallet": "BJZJ..." }
    }
  ]
}
```

### Detail pages (frontend route)

```
https://collectorcrypt.com/assets/solana/<nftAddress>
```

Pure frontend URL. Data comes from the same API + Solana RPC reads.

---

## How to update the endpoint list

1. Read the current bundle URL from the HTML (`<script src="/main.<hash>.js">`).
2. Run:
   ```powershell
   python tools/discover_endpoints.py > endpoints.txt
   ```
   Source: [tools/discover_endpoints.py](../tools/discover_endpoints.py)
3. Diff against the previous list; add new paths to [api-endpoints.md](api-endpoints.md).
4. Confirm whether a path is public:
   - `200` → inspect the response
   - `400` → path exists, parameters missing/wrong
   - `401` → auth required
   - `404` → no GET (often POST-only) or path is wrong

For a live authenticated probe (no orders placed):
```powershell
python tools/probe_live.py
```
Source: [tools/probe_live.py](../tools/probe_live.py)
