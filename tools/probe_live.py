"""Read-only verification of the assumed CollectorCrypt trading API shapes.

The live trader is built on a set of **reverse-engineered, unverified**
request/response shapes (every spot marked "ASSUMED" in ``docs/api.md``):
the SIWS handshake, ``me``, ``checkListingStatus``, the account endpoints and
the ``marketplace/buy`` unsigned-transaction response.

This script confirms those shapes against the **real** API **without ever
placing an order or spending money**. It:

* runs the SIWS handshake and reports exactly which response keys carry the
  nonce, the bearer token, the expiry and the account id;
* calls ``me`` to confirm the auth header is accepted and inspect the profile
  shape (account id location);
* calls ``checkListingStatus`` on a real listing and prints the **status
  vocabulary** so we can compare it to the confirmed/filled/cancelled synonyms
  the executor matches on;
* calls ``account/{id}/listings`` to confirm the path substitution;
* OPTIONALLY (explicit opt-in via ``--dry-buy-nft``) calls ``marketplace/buy``
  to inspect the **unsigned transaction** response shape. It never signs and
  never broadcasts — nothing settles on-chain.

Nothing here signs a transaction or calls ``marketplace/broadcast``. The only
local signing performed is ``sign_message`` for the SIWS login challenge, which
moves no funds. Secrets, tokens and signatures are redacted from all output.

Usage (PowerShell, from the repo root with the venv active)::

    # full read-only probe (SIWS login + reads), auto-discovers a listing id:
    & ".venv\\Scripts\\python.exe" tools/probe_live.py

    # probe a specific listing's status endpoint:
    & ".venv\\Scripts\\python.exe" tools/probe_live.py --listing-id v2_514hm3kZDf8JSkti

    # also inspect the unsigned-buy response shape (NO sign, NO broadcast):
    & ".venv\\Scripts\\python.exe" tools/probe_live.py --dry-buy-nft <nftAddress>

    # skip SIWS and use a pre-obtained token instead (TRADER_CC_TOKEN):
    & ".venv\\Scripts\\python.exe" tools/probe_live.py --skip-siws

Requires ``TRADER_WALLET_SECRET`` (for SIWS) or ``TRADER_CC_TOKEN`` (with
``--skip-siws``) in the environment / ``.env``. Writes a redacted findings
report to ``docs/api_probe_results.md``.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectorcrypt import config as app_config  # noqa: E402
from collectorcrypt.api import CCClient  # noqa: E402
from collectorcrypt.trader import config as trader_config  # noqa: E402
from collectorcrypt.trader.auth import StaticTokenProvider  # noqa: E402
from collectorcrypt.trader.ccapi import (  # noqa: E402
    CCApiError, CCTradingClient, redact,
)
from collectorcrypt.trader.siws import (  # noqa: E402
    EP_SIWS_AUTH, EP_SIWS_INIT, _extract_expiry, _extract_token,
)
from collectorcrypt.trader.wallet import Wallet, WalletError  # noqa: E402

OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "api_probe_results.md"

_SENSITIVE = ("token", "secret", "signature", "authorization", "bearer",
              "jwt", "password", "cookie", "signedtransaction", "privatekey")


# --------------------------------------------------------------------------- #
# Shape description (redacted)
# --------------------------------------------------------------------------- #
def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SENSITIVE)


def describe(value: Any, key: str = "", depth: int = 0) -> Any:
    """Return a redacted, type-annotated description of a JSON value.

    Key names and types are preserved so we can verify field names; sensitive
    values are masked. Short strings (e.g. a status word) are shown verbatim
    because the status vocabulary is exactly what we need to confirm.
    """
    if key and _is_sensitive(key):
        return "<redacted>"
    if isinstance(value, dict):
        if depth >= 4:
            return f"<dict {len(value)} keys>"
        return {k: describe(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        return [describe(value[0], key, depth + 1), f"... ({len(value)} items)"]
    if isinstance(value, str):
        if len(value) <= 48:
            return value
        return f"<str len={len(value)}>"
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    return f"<{type(value).__name__}>"


# --------------------------------------------------------------------------- #
# Report accumulation
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def h(self, text: str) -> None:
        self.lines.append(f"\n## {text}\n")
        print(f"\n=== {text} ===")

    def ok(self, text: str) -> None:
        self.lines.append(f"- ✅ {text}")
        print(f"  [OK]   {text}")

    def warn(self, text: str) -> None:
        self.lines.append(f"- ⚠️ {text}")
        print(f"  [WARN] {text}")

    def fail(self, text: str) -> None:
        self.lines.append(f"- ❌ {text}")
        print(f"  [FAIL] {text}")

    def note(self, text: str) -> None:
        self.lines.append(f"- {text}")
        print(f"         {text}")

    def block(self, label: str, value: Any) -> None:
        import json
        rendered = json.dumps(describe(value), indent=2, default=str)
        self.lines.append(f"\n**{label}**\n\n```jsonc\n{rendered}\n```")
        print(f"  {label}:")
        for line in rendered.splitlines():
            print(f"      {line}")

    def write(self, path: Path) -> None:
        header = (
            "# Live API probe results\n\n"
            f"> Generated by `tools/probe_live.py` on "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.\n"
            "> Read-only: no orders were placed and nothing was broadcast.\n"
            "> Secrets, tokens and signatures are redacted.\n"
        )
        path.write_text(header + "\n".join(self.lines) + "\n", encoding="utf-8")
        print(f"\nReport written to {path}")


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def probe_siws(rep: Report, wallet: Wallet, app_id: str,
               http: requests.Session) -> str:
    """Run the SIWS handshake with step-by-step shape diagnostics.

    Returns the bearer token on success, or "" on failure. Only signs the login
    challenge message (moves no funds).
    """
    rep.h("SIWS handshake (api/v1/siws/init + authenticate)")
    base = app_config.API_BASE.rstrip("/")
    headers = {"privy-app-id": app_id} if app_id else {}
    if not app_id:
        rep.warn("TRADER_PRIVY_APP_ID is not set; sending init without "
                 "privy-app-id header (the server may require it).")

    # Step 1: init -> nonce / message
    try:
        r = http.post(f"{base}/{EP_SIWS_INIT}", json={"address": wallet.address},
                      headers=headers, timeout=app_config.REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        rep.fail(f"init request failed (network): {exc}")
        return ""
    rep.note(f"init HTTP {r.status_code} at {EP_SIWS_INIT}")
    init = _json(r)
    if not isinstance(init, dict):
        rep.fail(f"init returned non-JSON / unexpected body (HTTP {r.status_code}). "
                 "The endpoint path or method may be wrong.")
        rep.block("init raw (redacted)", init)
        return ""
    rep.block("init response shape", init)
    nonce_key = _first_present(init, ("nonce", "challenge"))
    msg_key = _first_present(init, ("message",))
    if nonce_key:
        rep.ok(f"nonce found under key '{nonce_key}'")
    else:
        rep.warn("no 'nonce'/'challenge' key in init response — verify the "
                 "real nonce field name.")
    if msg_key:
        rep.ok(f"server provided a ready-to-sign 'message' (key '{msg_key}')")
    else:
        rep.note("no server 'message'; the client builds the SIWS message "
                 "locally (verify CC accepts that format).")

    # Build the message exactly like the provider would, then sign it.
    nonce = str(init.get("nonce") or init.get("challenge") or "").strip()
    message = init.get("message")
    if not (isinstance(message, str) and message.strip()):
        message = _local_siws_message(wallet.address, nonce or "probe-nonce")
    try:
        signature = wallet.sign_message(message.encode("utf-8"))
        rep.ok("signed the login challenge locally (no funds moved).")
    except WalletError as exc:
        rep.fail(f"could not sign the challenge: {exc}")
        return ""

    # Step 2: authenticate -> token / expiry / account
    payload = {
        "address": wallet.address,
        "message": message,
        "signature": signature,
        "nonce": nonce,
        "walletClientType": "solana",
    }
    rep.note(f"authenticate request body keys: {sorted(payload)}")
    try:
        r = http.post(f"{base}/{EP_SIWS_AUTH}", json=payload, headers=headers,
                      timeout=app_config.REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        rep.fail(f"authenticate request failed (network): {exc}")
        return ""
    rep.note(f"authenticate HTTP {r.status_code} at {EP_SIWS_AUTH}")
    auth = _json(r)
    if not isinstance(auth, dict):
        rep.fail("authenticate returned non-JSON / unexpected body. "
                 "Verify the path, headers and request body fields.")
        rep.block("authenticate raw (redacted)", auth)
        return ""
    rep.block("authenticate response shape", auth)

    token = _extract_token(auth)
    if token:
        rep.ok("bearer token extracted (our _extract_token keys matched).")
    else:
        rep.fail("no token found by _extract_token — the token field name is "
                 "different from all assumed keys. Inspect the shape above.")
    expiry = _extract_expiry(auth)
    rep.note(f"expiry resolved to epoch {expiry:.0f} "
             f"({datetime.fromtimestamp(expiry, timezone.utc):%Y-%m-%d %H:%M UTC}); "
             "if this is exactly +1h it likely fell back to the default TTL.")
    acct = str(auth.get("account_id") or auth.get("accountId")
               or (auth.get("user") or {}).get("id") or "")
    if acct:
        rep.ok(f"account id found (len={len(acct)}).")
    else:
        rep.warn("no account id in the authenticate response — account-scoped "
                 "reads (account/{id}/listings) will need another source.")
    return token


def probe_me(rep: Report, client: CCTradingClient) -> str:
    """Confirm the auth header is accepted and return the account id if present."""
    rep.h("me (api/v1/users/me)")
    try:
        data = client.me()
    except CCApiError as exc:
        rep.fail(f"me() failed: {type(exc).__name__}: {exc}")
        return ""
    rep.ok("auth header accepted (HTTP 2xx).")
    rep.block("me response shape", data)
    acct = str(data.get("id") or data.get("account_id")
               or (data.get("user") or {}).get("id") or "")
    if acct:
        rep.ok(f"account id resolved from me() (len={len(acct)}).")
    else:
        rep.warn("could not locate an account id field in me().")
    return acct


def probe_listing_status(rep: Report, client: CCTradingClient,
                         listing_id: str) -> None:
    rep.h("checkListingStatus")
    if not listing_id:
        rep.warn("no listing id available to probe (pass --listing-id or let "
                 "auto-discovery find one).")
        return
    rep.note(f"probing listing id: {listing_id}")
    try:
        data = client.check_listing_status(listing_id)
    except CCApiError as exc:
        rep.fail(f"check_listing_status failed: {type(exc).__name__}: {exc}")
        return
    rep.ok("checkListingStatus accepted the 'id' query param (HTTP 2xx).")
    rep.block("checkListingStatus response shape", data)
    status = _find_status(data)
    if status:
        rep.ok(f"status value observed: '{status}' — compare against the "
               "confirmed/filled/cancelled synonyms in executor.py.")
    else:
        rep.warn("no obvious status field found; inspect the shape above to "
                 "confirm where the on-chain status is reported.")


def probe_account_listings(rep: Report, client: CCTradingClient,
                           account_id: str) -> None:
    rep.h("account/{id}/listings")
    if not account_id:
        rep.warn("no account id resolved; skipping account_listings probe.")
        return
    try:
        data = client.account_listings(account_id)
    except CCApiError as exc:
        rep.fail(f"account_listings failed: {type(exc).__name__}: {exc}")
        return
    rep.ok("account/{id}/listings path accepted (HTTP 2xx).")
    rep.block("account_listings response shape", data)


def probe_dry_buy(rep: Report, client: CCTradingClient, nft: str,
                  price: float, receipt_id: str) -> None:
    """Inspect the unsigned-tx response of marketplace/buy. NEVER signs/broadcasts."""
    rep.h("marketplace/buy — UNSIGNED tx shape (DRY: no sign, no broadcast)")
    rep.warn("This calls marketplace/buy to inspect the returned UNSIGNED "
             "transaction. It does NOT sign and does NOT broadcast, so nothing "
             "settles on-chain. Abort now (Ctrl+C) if that is not intended.")
    try:
        data = client.initiate_buy(nft=nft, price=price, receipt_id=receipt_id)
    except CCApiError as exc:
        rep.fail(f"initiate_buy failed: {type(exc).__name__}: {exc}")
        rep.note("A 4xx here often means the request body field names are wrong "
                 "(verify nftAddress/price/currency/receiptId against DevTools).")
        return
    rep.ok("marketplace/buy returned 2xx.")
    rep.block("initiate_buy response shape", data)
    tx_key = _first_present(
        data, ("transaction", "tx", "serializedTransaction", "unsignedTransaction",
               "txn", "data"))
    if tx_key:
        rep.ok(f"unsigned transaction found under key '{tx_key}'.")
    else:
        rep.warn("no transaction field matched the assumed keys — inspect the "
                 "shape above to confirm where the unsigned tx is returned.")
    rep.note("NOT signing and NOT broadcasting — probe ends here by design.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"<non-json-body>": resp.text[:200]}


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in data and data[k] not in (None, "", [], {}):
            return k
    return ""


def _find_status(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("status", "state", "listingStatus", "onChainStatus"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
        for parent in ("data", "result", "listing"):
            child = data.get(parent)
            if isinstance(child, dict):
                nested = _find_status(child)
                if nested:
                    return nested
    return ""


def _local_siws_message(address: str, nonce: str) -> str:
    issued = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "collectorcrypt.com wants you to sign in with your Solana account:\n"
        f"{address}\n\n"
        "Sign in to CollectorCrypt\n\n"
        "URI: https://collectorcrypt.com\n"
        "Version: 1\n"
        "Chain ID: solana:mainnet\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued}"
    )


def _discover_listing_id(rep: Report) -> tuple[str, str, float]:
    """Find a real (receiptId, nftAddress, price) from the public marketplace."""
    try:
        page = CCClient().fetch_marketplace_page(1, 30)
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        rep.warn(f"could not auto-discover a listing: {exc}")
        return "", "", 0.0
    for card in page.get("filterNFtCard", []) or []:
        listing = card.get("listing") or {}
        receipt = str(listing.get("receiptId") or "")
        nft = str(card.get("nftAddress") or "")
        price = float(listing.get("price") or 0.0)
        if receipt and nft:
            rep.note(f"auto-discovered listing receiptId for status probe "
                     f"(nft len={len(nft)}, price={price}).")
            return receipt, nft, price
    rep.warn("no usable listing found on marketplace page 1.")
    return "", "", 0.0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listing-id", default="",
                    help="receiptId to probe checkListingStatus (auto-discovered "
                         "if omitted).")
    ap.add_argument("--dry-buy-nft", default="",
                    help="NFT address to inspect the marketplace/buy UNSIGNED "
                         "tx shape (explicit opt-in; never signs/broadcasts).")
    ap.add_argument("--dry-buy-price", type=float, default=0.0,
                    help="Price for the dry-buy probe (defaults to the listing "
                         "price when the nft is auto-discovered).")
    ap.add_argument("--dry-buy-receipt", default="",
                    help="receiptId for the dry-buy probe.")
    ap.add_argument("--skip-siws", action="store_true",
                    help="Use TRADER_CC_TOKEN (static) instead of the SIWS "
                         "handshake.")
    ap.add_argument("--out", default=str(OUT_PATH),
                    help="Path to write the findings report.")
    args = ap.parse_args()

    rep = Report()
    rep.h("Environment")
    cfg = trader_config.load_config()
    rep.note(f"API base: {app_config.API_BASE}")
    rep.note(f"auth_provider: {cfg.auth_provider!r}  live: {cfg.live}")
    rep.note(f"wallet configured: {bool(cfg.wallet_address or cfg.wallet_secret)} "
             f"(can_sign: {bool(cfg.wallet_secret)})")

    http = requests.Session()
    http.headers["User-Agent"] = app_config.USER_AGENT
    http.headers["Accept"] = "application/json"

    # Build the wallet (needed for SIWS signing).
    wallet: Wallet | None = None
    if cfg.wallet_address or cfg.wallet_secret:
        try:
            wallet = Wallet(cfg.rpc_url, address=cfg.wallet_address,
                            secret=cfg.wallet_secret)
        except WalletError as exc:
            rep.fail(f"wallet construction failed: {exc}")

    # Establish a session token.
    token = ""
    if args.skip_siws:
        rep.h("Auth via static token (TRADER_CC_TOKEN)")
        if cfg.cc_token:
            token = cfg.cc_token
            rep.ok("using TRADER_CC_TOKEN (skipping SIWS handshake).")
        else:
            rep.fail("--skip-siws set but TRADER_CC_TOKEN is empty.")
    elif wallet is not None and wallet.can_sign:
        token = probe_siws(rep, wallet, cfg.privy_app_id, http)
    else:
        rep.h("Auth")
        rep.fail("No signing wallet (TRADER_WALLET_SECRET) for SIWS and "
                 "--skip-siws not set. Cannot establish a session.")

    if not token:
        rep.warn("No session token — skipping authenticated read probes.")
        rep.write(Path(args.out))
        return 1

    # Authenticated client using the obtained token.
    client = CCTradingClient(
        session_provider=StaticTokenProvider(token), http=http)

    account_id = probe_me(rep, client)

    # Listing status probe (auto-discover an id if not provided).
    listing_id = args.listing_id
    disc_nft, disc_price = "", 0.0
    if not listing_id or (args.dry_buy_nft == "auto"):
        listing_id_disc, disc_nft, disc_price = _discover_listing_id(rep)
        listing_id = listing_id or listing_id_disc
    probe_listing_status(rep, client, listing_id)

    probe_account_listings(rep, client, account_id)

    # Optional unsigned-buy shape probe (explicit opt-in).
    if args.dry_buy_nft:
        nft = disc_nft if args.dry_buy_nft == "auto" else args.dry_buy_nft
        price = args.dry_buy_price or disc_price
        probe_dry_buy(rep, client, nft, price, args.dry_buy_receipt or listing_id)
    else:
        rep.h("marketplace/buy — skipped")
        rep.note("Pass --dry-buy-nft <addr> (or 'auto') to inspect the unsigned "
                 "tx shape. It never signs or broadcasts.")

    rep.write(Path(args.out))
    rep.h("Summary")
    rep.note("Review the response shapes above and update docs/api.md, then "
             "ccapi.py / siws.py / executor.py where field names differ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
