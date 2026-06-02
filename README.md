# CollectorCrypt Karten Viewer

Flask-App rund um die öffentliche `api.collectorcrypt.com` mit zwei Ansichten:

| Route        | Zweck                                                                 |
| ------------ | --------------------------------------------------------------------- |
| `/`          | Marketplace-Browser mit Filtern, Observe-Liste, Karten-/Listenmodus.  |
| `/deals`     | Hintergrund-Scanner, der Listings vs. CC-Insured-Value vergleicht.    |
| `/api/card/<nft>` | Einzelne Karte frisch aus der API (für Observe-Live-Refresh).    |

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Starten

```powershell
python app.py
```

Dann <http://127.0.0.1:5000> öffnen. Alternativ `start.bat`.

## Projektstruktur

```
app.py                       # Entry-Point – baut die App via Factory.
collectorcrypt/
  __init__.py                # exportiert create_app
  config.py                  # URLs, Timeouts, Retry-Policy, Limits
  normalize.py               # Karten-Parsing, Formatierung, FX-Umrechnung
  api.py                     # CCClient – HTTP + In-Memory-Cache + Retry
  scanner.py                 # ScanManager – Hintergrund-Worker (Deals)
  web.py                     # Flask-Factory + Blueprints (views, api)
templates/
  base.html                  # Layout, lädt CSS + Core-JS
  _card.html                 # Wiederverwendbare Marketplace-Karte
  index.html                 # Marketplace + Observe
  deals.html                 # Deals-Scanner (Cards + List)
static/
  css/app.css                # Komplettes Design (Cards, Listen, Stats, Pills, …)
  js/util.js                 # Formatter (USD/Preis, escapeHtml)
  js/lightbox.js             # Karten-Vorschau (Flip + Parallax)
  js/observe-store.js        # Persistente Observe-Daten (localStorage)
  js/observe-cards.js        # Observe-Buttons + Rebuild + API-Refresh
  js/insured.js              # Insured-Value-Footer
  js/view-toggle.js          # Karten/Listen-Modus (persistent)
  js/filters.js              # Marketplace-Filterleiste
  js/marketplace.js          # Bindings für /
  js/deals.js                # Bindings für /deals (inkl. Poll-Loop)
tools/
  discover_endpoints.py      # Reverse-Engineering der CC-API
```

## Architekturnotizen

* **Trennung Daten/Transport/UI.** `normalize.py` ist rein funktional und
  kennt weder Flask noch HTTP. `api.py` macht ausschließlich Transport.
  `scanner.py` orchestriert den Worker und nutzt beides.
* **Single Source of Truth fürs Karten-Layout.** Marketplace und Deals
  nutzen dieselbe `.card`-Struktur, dasselbe CSS, dieselben Observe- und
  Lightbox-Module. Deals rendert die Karten clientseitig (live), der
  Marketplace serverseitig – funktional identisch.
* **Karten- und Listen-Modus** sind über eine einzige CSS-Klasse
  (`grid.view-list`) realisiert und gelten für beide Seiten gleich.
