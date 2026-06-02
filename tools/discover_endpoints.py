"""Extrahiert API-Pfade aus dem CollectorCrypt-Frontend-Bundle.

Sucht gezielt nach typischen API-Pfad-Literalen (marketplace, listings, cards,
account, auth, blockchain, ...) und filtert CSS-Klassen heraus. Ergebnis ist
eine sortierte Liste eindeutiger Pfade auf stdout.

Aufruf:
    python tools/discover_endpoints.py [bundle_url]

Ohne Argument wird die aktuelle main.<hash>.js-URL aus dem Index ermittelt.
"""
from __future__ import annotations

import re
import sys
from urllib.parse import urljoin

import requests

ROOT = "https://collectorcrypt.com/"
UA = {"User-Agent": "Mozilla/5.0 (endpoint-discovery)"}

PATH_RE = re.compile(
    r'"((?:/?api/)?(?:'
    r'marketplace|listings?|cards?|assets?|account|auth|users?|offers?|'
    r'grading|shipping|blockchain|burn|redeem|buy|pay|follows?|blocks?|'
    r'hidden-offers|comics|games|merch|sealed|search|notifications?|'
    r'contact|outbound-shipment|verify_nft_card|calcListingFee|'
    r'checkListingStatus|createAcceptOfferTx[A-Za-z0-9]*'
    r')[A-Za-z0-9_/-]*)"'
)


def find_bundle_url() -> str:
    html = requests.get(ROOT, headers=UA, timeout=30).text
    m = re.search(r'src="(/main\.[a-f0-9]+\.js)"', html)
    if not m:
        raise SystemExit("main.<hash>.js im HTML nicht gefunden.")
    return urljoin(ROOT, m.group(1))


def extract(js: str) -> list[str]:
    out: set[str] = set()
    for m in PATH_RE.finditer(js):
        p = m.group(1).strip("/")
        if not p or " " in p or len(p) > 80 or "__" in p:
            continue
        out.add(p)
    return sorted(out)


def main() -> None:
    bundle = sys.argv[1] if len(sys.argv) > 1 else find_bundle_url()
    print(f"# Bundle: {bundle}", file=sys.stderr)
    js = requests.get(bundle, headers=UA, timeout=60).text
    paths = extract(js)
    print(f"# {len(paths)} Pfade gefunden", file=sys.stderr)
    for p in paths:
        print(p)


if __name__ == "__main__":
    main()
