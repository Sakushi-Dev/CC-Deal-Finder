# CC-Deal-Finder

> Flask-based marketplace browser and deal scanner for the public `api.collectorcrypt.com`.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.0%2B-black.svg)](https://flask.palletsprojects.com/)
[![Status](https://img.shields.io/badge/status-active-success.svg)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## Table of contents

- [Features](#features)
- [Routes](#routes)
- [Installation](#installation)
- [Run](#run)
- [Project structure](#project-structure)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

---

## Features

- Marketplace browser with filters, card/list view and lightbox preview
- Observe list with live refresh via API and persistent storage (`localStorage`)
- Deal scanner that compares current listings against the CC insured value
- In-memory cache with retry policy for stable API requests
- Unified card layout for marketplace and deals (single source of truth)

## Routes

| Route               | Purpose                                                                |
| ------------------- | ---------------------------------------------------------------------- |
| `/`                 | Marketplace browser with filters, observe list, card/list view        |
| `/deals`            | Background scanner comparing listings vs. CC insured value             |
| `/api/card/<nft>`   | Single card fresh from the API (used for observe live refresh)         |

## Installation

**Requirements:** Python 3.10+ and `pip`.

```powershell
# Clone the repository
git clone https://github.com/Sakushi-Dev/CC-Deal-Finder.git
cd CC-Deal-Finder

# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

> **Linux/macOS:** use `source .venv/bin/activate` instead of the PowerShell activation.

## Run

```powershell
python app.py
```

Then open <http://127.0.0.1:5000> in the browser.

Alternatively, on Windows double-click [`start.bat`](start.bat) — starts the app minimized and opens the browser automatically.

## Project structure

```text
.
├── app.py                       # Entry point – builds the app via the factory
├── requirements.txt             # Flask + requests
├── start.bat                    # Windows launcher
├── collectorcrypt/
│   ├── __init__.py              # exports create_app
│   ├── config.py                # URLs, timeouts, retry policy, limits
│   ├── normalize.py             # card parsing, formatting, FX conversion
│   ├── api.py                   # CCClient – HTTP + in-memory cache + retry
│   ├── scanner.py               # ScanManager – background worker (deals)
│   └── web.py                   # Flask factory + blueprints (views, api)
├── templates/
│   ├── base.html                # layout, loads CSS + core JS
│   ├── _card.html               # reusable marketplace card
│   ├── index.html               # marketplace + observe
│   └── deals.html               # deals scanner (cards + list)
├── static/
│   ├── css/app.css              # full design
│   └── js/
│       ├── util.js              # formatters (USD/price, escapeHtml)
│       ├── lightbox.js          # card preview (flip + parallax)
│       ├── observe-store.js     # persistent observe data (localStorage)
│       ├── observe-cards.js     # observe buttons + rebuild + API refresh
│       ├── insured.js           # insured-value footer
│       ├── view-toggle.js       # card/list view (persistent)
│       ├── filters.js           # marketplace filter bar
│       ├── marketplace.js       # bindings for /
│       └── deals.js             # bindings for /deals (incl. poll loop)
├── tools/
│   └── discover_endpoints.py    # reverse-engineers the CC API
└── docs/
    └── api.md                   # API documentation
```

## Architecture

- **Separation of data / transport / UI**
  `normalize.py` is purely functional and knows neither Flask nor HTTP.
  `api.py` does transport only. `scanner.py` orchestrates the worker and uses both.

- **Single source of truth for the card layout**
  Marketplace and deals use the same `.card` structure, the same CSS and the same observe/lightbox modules.
  Deals renders cards client-side (live), the marketplace server-side — functionally identical.

- **Card and list view**
  Implemented via a single CSS class (`grid.view-list`) and used identically on both pages.

## Development

Further API details are in [`docs/api.md`](docs/api.md).

To discover previously unknown endpoints:

```powershell
python tools/discover_endpoints.py
```

## License

This project is licensed under the [MIT License](LICENSE).
