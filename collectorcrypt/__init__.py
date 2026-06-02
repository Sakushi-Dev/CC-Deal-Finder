"""CollectorCrypt-Viewer – modulare Anwendung.

Pakete:
    config     – Konstanten (URLs, Timeouts, Retry-Policy).
    normalize  – Parsing & Formatierung der API-Karten in unser UI-Schema.
    api        – HTTP-Client für die CollectorCrypt-/Coinbase-APIs (+Cache).
    scanner    – Hintergrund-Scanner für die Deals-Suche.
    web        – Flask-App-Factory + Routen.
"""

from .web import create_app

__all__ = ["create_app"]
