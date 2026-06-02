"""CollectorCrypt viewer – modular application.

Packages:
    config     – Constants (URLs, timeouts, retry policy).
    normalize  – Parses & formats API cards into our UI schema.
    api        – HTTP client for the CollectorCrypt / Coinbase APIs (+cache).
    scanner    – Background scanner for the deals search.
    web        – Flask app factory + routes.
"""

from .web import create_app

__all__ = ["create_app"]
