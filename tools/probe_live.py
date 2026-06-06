"""Read-only live-readiness probe for CollectorCrypt.

Verifies the real API against our assumed request/response shapes WITHOUT
placing any orders or spending any money. All calls are GET / read-only or
auth-only (no marketplace writes).

What this checks
----------------
1. SIWS handshake  — does the Privy SIWS flow work end-to-end?
   Confirms: init endpoint + path, response nonce key, authenticate endpoint +
   path, token key(s) in response, expiry key(s), account_id key.

2. me()  — does the established session return a valid profile?
   Confirms: auth header accepted, user object shape, account id present.

3. checkListingStatus  — does querying a known listing return anything useful?
   Confirms: endpoint path, query param name, status vocabulary.
   Requires a listing id from the marketplace (passed via --listing-id or
   discovered automatically from the first open listing).

4. account_listings  — do account endpoints work?
   Confirms: endpoint path with account_id substitution, listing shape.

Usage
-----
    python tools/probe_live.py [--listing-id <id>]

The script reads the same env vars the trader uses:
    TRADER_WALLET_SECRET   — base58 keypair secret (required)
    TRADER_WALLET_ADDRESS  — public key (required)
    TRADER_PRIVY_APP_ID    — Privy app id (optional but recommended)
    TRADER_CC_TOKEN        — static token to skip SIWS (optional)
    TRADER_AUTH_PROVIDER   — 'privy' (default) or 'static'

Output
------
Every section prints:
  [OK]  what was confirmed
  [?]   something unexpected, not fatal
  [FAIL] unexpected error or wrong shape

A summary at the end lists which assumptions are now confirmed and what still
needs verification. Findings are written to docs/api_probe_results.md.

Security
--------
No private key, token or signature is ever printed. The script follows the
same redaction rules as the trader (ccapi.redact).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── bootstrap the package path ─────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env if present (same as the trader)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from collectorcrypt.trader.ccapi import (
    CCApiError, CCAuthError, CCTradingClient, redact,
)
from collectorcrypt.trader.config import load_config
from collectorcrypt.trader.siws import (
    PrivySiwsProvider, _extract_token, _extract_expiry, make_session_provider,
)
from collectorcrypt.trader.wallet import Wallet, WalletError
from collectorcrypt import api as cc_public_api

# ── colour helpers (no deps) ────────────────────────────────────────────────
_USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

OK   = lambda t: print(_c("32", "[OK]  ") + t)      # green
WARN = lambda t: print(_c("33", "[?]   ") + t)      # yellow
FAIL = lambda t: print(_c("31", "[FAIL]") + " " + t) # red
INFO = lambda t: print("      " + t)
HEAD = lambda t: print("\n" + _c("1;36", f"── {t} "))


# ── result collector ────────────────────────────────────────────────────────
_confirmed: list[str] = []
_warnings: list[str]  = []
_failures: list[str]  = []

def _ok(msg: str)   -> None: _confirmed.append(msg); OK(msg)
def _warn(msg: str) -> None: _warnings.append(msg);  WARN(msg)
def _fail(msg: str) -> None: _failures.append(msg);  FAIL(msg)


# ── helpers ─────────────────────────────────────────────────────────────────
def _pretty(data: Any) -> str:
    safe = redact(data)
    return json.dumps(safe, indent=2, ensure_ascii=False)[:2000]


def _get_listing_id_from_marketplace() -> str:
    """Grab the receiptId of the first open CC listing (no auth needed)."""
    try:
        client = cc_public_api.CCClient()
        page = client.fetch_marketplace_page(page=1, step=5)
        cards = page.get("filterNFtCard") or []
        for card in cards:
            lst = card.get("listing") or {}
            rid = lst.get("receiptId") or lst.get("id") or ""
            if rid:
                return rid
        return ""
    except Exception as exc:  # noqa: BLE001
        WARN(f"Could not auto-discover a listing id: {exc}")
        return ""


# ═══════════════════════════════════════════════════════════════════════════ #
#  Section 1 – SIWS handshake (raw, step-by-step for max diagnostics)
# ═══════════════════════════════════════════════════════════════════════════ #
def probe_siws(wallet: Wallet, cfg) -> "str | None":
    """Run the full SIWS handshake and return the token on success."""
    HEAD("1 / SIWS handshake")
    import requests as _req
    import secrets as _sec

    from collectorcrypt.trader.siws import EP_SIWS_INIT, EP_SIWS_AUTH
    from collectorcrypt import config as app_cfg

    base = app_cfg.API_BASE.rstrip("/")
    http = _req.Session()
    http.headers["User-Agent"] = app_cfg.USER_AGENT
    http.headers["Accept"] = "application/json"
    headers: dict[str, str] = {}
    if cfg.privy_app_id:
        headers["privy-app-id"] = cfg.privy_app_id
        INFO(f"Using privy-app-id: {cfg.privy_app_id[:8]}…")

    address = wallet.address
    INFO(f"Wallet address: {address}")

    # Step 1a – init
    INFO(f"POST {base}/{EP_SIWS_INIT}")
    try:
        r = http.post(f"{base}/{EP_SIWS_INIT}",
                      json={"address": address},
                      headers=headers, timeout=15)
        INFO(f"Status: {r.status_code}")
        try:
            init_data = r.json()
        except ValueError:
            init_data = {}
            _fail(f"init returned non-JSON (status {r.status_code}): {r.text[:300]}")
            return None
        INFO(f"Response keys: {list(init_data.keys())}")
        if r.status_code >= 400:
            _fail(f"init failed ({r.status_code}): {_pretty(init_data)}")
            return None
    except Exception as exc:  # noqa: BLE001
        _fail(f"init network error: {exc}")
        return None

    nonce = str(init_data.get("nonce") or init_data.get("challenge") or "").strip()
    prebuilt_msg = init_data.get("message")

    if nonce:
        _ok(f"init → nonce key present ('{list(k for k in init_data if 'nonce' in k.lower() or 'challenge' in k.lower())}')")
    else:
        _warn("init → no 'nonce'/'challenge' key in response — using random fallback")
        nonce = _sec.token_hex(16)

    if isinstance(prebuilt_msg, str) and prebuilt_msg.strip():
        _ok("init → server supplied a ready-made message to sign")
        message = prebuilt_msg
    else:
        _warn("init → no ready-made 'message' — building SIWS message locally (assumed format)")
        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = (
            f"collectorcrypt.com wants you to sign in with your Solana account:\n"
            f"{address}\n\n"
            f"Sign in to CollectorCrypt\n\n"
            f"URI: https://collectorcrypt.com\n"
            f"Version: 1\n"
            f"Chain ID: solana:mainnet\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}"
        )

    # Step 1b – sign message locally
    try:
        signature = wallet.sign_message(message.encode("utf-8"))
        _ok("wallet.sign_message succeeded (signature NOT printed)")
    except WalletError as exc:
        _fail(f"wallet.sign_message failed: {exc}")
        return None

    # Step 1c – authenticate
    auth_payload = {
        "address": address,
        "message": message,
        "signature": signature,
        "nonce": nonce,
        "walletClientType": "solana",
    }
    INFO(f"POST {base}/{EP_SIWS_AUTH}")
    INFO(f"Payload keys: {list(auth_payload.keys())} (signature+message redacted)")
    try:
        r2 = http.post(f"{base}/{EP_SIWS_AUTH}",
                       json=auth_payload, headers=headers, timeout=15)
        INFO(f"Status: {r2.status_code}")
        try:
            auth_data = r2.json()
        except ValueError:
            auth_data = {}
            _fail(f"authenticate returned non-JSON (status {r2.status_code}): {r2.text[:300]}")
            return None
        INFO(f"Response top-level keys: {list(auth_data.keys())}")
        if r2.status_code >= 400:
            _fail(f"authenticate failed ({r2.status_code}): {_pretty(auth_data)}")
            return None
    except Exception as exc:  # noqa: BLE001
        _fail(f"authenticate network error: {exc}")
        return None

    # Inspect token location
    token = _extract_token(auth_data)
    if token:
        for key in ("token", "access_token", "accessToken", "jwt",
                    "privy_access_token", "session_token"):
            if auth_data.get(key):
                _ok(f"authenticate → token found at top-level key '{key}'")
                break
        else:
            for parent in ("session", "data"):
                child = auth_data.get(parent)
                if isinstance(child, dict) and _extract_token(child):
                    _ok(f"authenticate → token nested under '{parent}'")
                    break
    else:
        _fail("authenticate → NO token found in response (check docs/api.md assumed keys)")
        INFO(f"Full response (redacted): {_pretty(auth_data)}")
        return None

    # Inspect expiry location
    for key in ("expires_at", "expiresAt", "exp", "expires_in", "expiresIn", "ttl"):
        if auth_data.get(key) is not None:
            _ok(f"authenticate → expiry found at key '{key}' = {auth_data[key]!r}")
            break
    else:
        _warn("authenticate → no expiry key found; will use 1h default TTL")

    # Inspect account_id location
    account_id = (
        str(auth_data.get("account_id") or auth_data.get("accountId") or
            (auth_data.get("user") or {}).get("id") or "")
    )
    if account_id:
        _ok(f"authenticate → account_id = {account_id}")
    else:
        _warn("authenticate → no account_id in response (will be empty string)")

    _ok("SIWS handshake complete")
    return token


# ═══════════════════════════════════════════════════════════════════════════ #
#  Section 2 – me() via CCTradingClient
# ═══════════════════════════════════════════════════════════════════════════ #
def probe_me(client: CCTradingClient) -> "str":
    """Call me() and return the account id."""
    HEAD("2 / api/v1/users/me")
    try:
        data = client.me()
        INFO(f"Response keys: {list(data.keys())}")
        acct_id = str(data.get("id") or data.get("account_id") or
                      data.get("accountId") or "")
        if acct_id:
            _ok(f"me() → account id = {acct_id}")
        else:
            _warn(f"me() → no 'id'/'account_id' key; keys = {list(data.keys())}")
        wallet_field = data.get("wallet") or data.get("wallets") or data.get("address")
        if wallet_field:
            _ok(f"me() → wallet field present (key confirmed)")
        return acct_id
    except CCAuthError as exc:
        _fail(f"me() auth error: {exc}")
    except CCApiError as exc:
        _fail(f"me() api error ({exc.status}): {exc}")
    return ""


# ═══════════════════════════════════════════════════════════════════════════ #
#  Section 3 – checkListingStatus
# ═══════════════════════════════════════════════════════════════════════════ #
def probe_check_listing_status(client: CCTradingClient, listing_id: str) -> None:
    HEAD("3 / checkListingStatus")
    if not listing_id:
        _warn("No listing id available — skipping checkListingStatus probe")
        INFO("Pass --listing-id <receiptId> to test this endpoint")
        return
    INFO(f"Checking listing id: {listing_id}")
    try:
        data = client.check_listing_status(listing_id)
        INFO(f"Response keys: {list(data.keys())}")
        INFO(f"Response (redacted): {_pretty(data)}")

        # Look for a status field
        status_val = (data.get("status") or data.get("listingStatus") or
                      data.get("state") or "")
        if status_val:
            _ok(f"checkListingStatus → status field = '{status_val}'")
            # Check against our assumed vocabulary
            assumed_ok = {"confirmed", "filled", "cancelled", "canceled",
                          "withdrawn", "expired", "listed", "pending",
                          "open", "accepted", "rejected", "removed", "delisted"}
            if str(status_val).lower() in assumed_ok:
                _ok(f"status value '{status_val}' is in assumed vocabulary")
            else:
                _warn(f"status value '{status_val}' is NOT in assumed vocabulary — update executor.py helpers")
        else:
            _warn(f"checkListingStatus → no 'status' key; keys = {list(data.keys())}")
            INFO("This may mean the response wraps the status differently")

    except CCApiError as exc:
        _fail(f"checkListingStatus failed ({exc.status}): {exc}")
        INFO("If this is a 400/404, the listing id or endpoint path may be wrong")


# ═══════════════════════════════════════════════════════════════════════════ #
#  Section 4 – account_listings
# ═══════════════════════════════════════════════════════════════════════════ #
def probe_account_listings(client: CCTradingClient, account_id: str) -> None:
    HEAD("4 / account/{id}/listings")
    if not account_id:
        _warn("No account id — skipping account_listings probe")
        return
    try:
        data = client.account_listings(account_id)
        INFO(f"Response type: {type(data).__name__}  keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
        INFO(f"Response (redacted): {_pretty(data)}")
        _ok("account_listings → endpoint reachable, shape logged above")
    except CCApiError as exc:
        _fail(f"account_listings failed ({exc.status}): {exc}")


# ═══════════════════════════════════════════════════════════════════════════ #
#  Section 5 – Dry-run: inspect buy prepare response shape (NO spend)
#              Only runs if --dry-buy-nft is given explicitly.
# ═══════════════════════════════════════════════════════════════════════════ #
def probe_buy_shape(client: CCTradingClient, nft: str,
                    price: float, receipt_id: str) -> None:
    HEAD("5 / marketplace/buy  [DRY PROBE — response shape only]")
    INFO("NOTE: this call INITIATES a buy prepare request.")
    INFO("The returned unsigned tx is inspected but NEVER signed or broadcast.")
    INFO(f"NFT: {nft}  price: {price} USDC  receiptId: {receipt_id}")
    try:
        data = client.initiate_buy(nft=nft, price=price, receipt_id=receipt_id)
        INFO(f"Response keys: {list(data.keys())}")
        INFO(f"Response (redacted): {_pretty(data)}")

        # Check for a transaction field
        tx_val = (data.get("transaction") or data.get("tx") or
                  data.get("serializedTransaction") or data.get("rawTransaction") or
                  data.get("unsignedTransaction") or "")
        if tx_val:
            _ok(f"marketplace/buy → unsigned tx field found (key confirmed)")
            # Check it looks like base64
            import base64
            try:
                raw = base64.b64decode(tx_val)
                _ok(f"tx value decodes as base64 ({len(raw)} bytes)")
            except Exception:  # noqa: BLE001
                _warn("tx value is NOT base64-decodable — may be base58 or hex")
        else:
            _warn(f"marketplace/buy → no tx field found; keys = {list(data.keys())}")

        # Check for receipt/offer/listing id
        for key in ("receiptId", "id", "offerId", "listingId", "orderId"):
            if data.get(key):
                _ok(f"marketplace/buy → id field '{key}' = {data[key]!r}")
                break
        else:
            _warn("marketplace/buy → no id field found in response")

    except CCApiError as exc:
        _fail(f"marketplace/buy failed ({exc.status}): {exc}")
        if exc.body:
            INFO(f"Error body: {_pretty(exc.body)}")


# ═══════════════════════════════════════════════════════════════════════════ #
#  Summary + write findings to docs/
# ═══════════════════════════════════════════════════════════════════════════ #
def write_results(args: argparse.Namespace) -> None:
    HEAD("Summary")
    print(f"  Confirmed : {len(_confirmed)}")
    print(f"  Warnings  : {len(_warnings)}")
    print(f"  Failures  : {len(_failures)}")

    lines = [
        f"# probe_live.py results — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Run args: {vars(args)}",
        "",
        "## Confirmed",
        *[f"- {c}" for c in _confirmed],
        "",
        "## Warnings (check manually)",
        *[f"- {w}" for w in _warnings],
        "",
        "## Failures",
        *[f"- {f}" for f in _failures],
        "",
        "## What to update in docs/api.md",
    ]
    if not _failures and not _warnings:
        lines.append("- All assumptions confirmed. Mark trading-flow shapes as VERIFIED.")
    else:
        if _failures:
            lines.append("- Fix failures before enabling live trading.")
        if _warnings:
            lines.append("- Review warnings; update assumed key names in ccapi.py / executor.py as needed.")

    out_path = ROOT / "docs" / "api_probe_results.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Results written to {out_path.relative_to(ROOT)}")

    if _failures:
        print(_c("31", "\n  ✗ PROBE FAILED — do NOT enable live trading yet"))
        sys.exit(1)
    elif _warnings:
        print(_c("33", "\n  ⚠  Probe passed with warnings — review before live trading"))
    else:
        print(_c("32", "\n  ✓ All checks passed"))


# ═══════════════════════════════════════════════════════════════════════════ #
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════ #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=textwrap.dedent("""\
            Read-only live-readiness probe for CollectorCrypt.
            Verifies API shapes WITHOUT spending any money.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--listing-id", default="",
        help="receiptId of a known open listing to probe checkListingStatus. "
             "Auto-discovered from the public marketplace if omitted.",
    )
    p.add_argument(
        "--dry-buy-nft", default="",
        help="NFT address to probe the marketplace/buy prepare endpoint. "
             "Only inspects the response — NEVER signs or broadcasts. "
             "Requires --dry-buy-price and --dry-buy-receipt.",
    )
    p.add_argument("--dry-buy-price", type=float, default=0.0,
                   help="Price in USDC for the dry buy probe.")
    p.add_argument("--dry-buy-receipt", default="",
                   help="receiptId for the dry buy probe.")
    p.add_argument(
        "--skip-siws", action="store_true",
        help="Skip the SIWS section and use TRADER_CC_TOKEN (static) instead.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # ── load config + wallet ────────────────────────────────────────────────
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        FAIL(f"load_config() failed: {exc}")
        sys.exit(1)

    secret = os.environ.get("TRADER_WALLET_SECRET", "")
    address = os.environ.get("TRADER_WALLET_ADDRESS", cfg.wallet_address or "")
    if not secret or not address:
        FAIL("TRADER_WALLET_SECRET and TRADER_WALLET_ADDRESS must be set.")
        INFO("These are env-only (never in trader_settings.json).")
        sys.exit(1)

    try:
        wallet = Wallet(address=address, secret=secret)
    except WalletError as exc:
        FAIL(f"Wallet init failed: {exc}")
        sys.exit(1)

    # ── section 1: SIWS (or skip if --skip-siws / static token) ────────────
    token: str | None = None
    if args.skip_siws or cfg.auth_provider == "static":
        HEAD("1 / SIWS handshake  [SKIPPED — using static token]")
        token = cfg.cc_token or os.environ.get("TRADER_CC_TOKEN", "")
        if token:
            _ok("Static token loaded (not printed)")
        else:
            _fail("--skip-siws set but TRADER_CC_TOKEN is empty")
    else:
        token = probe_siws(wallet, cfg)

    if not token:
        _fail("No auth token — cannot continue with authenticated probes")
        write_results(args)
        return

    # ── build a trading client using the confirmed token ────────────────────
    from collectorcrypt.trader.auth import StaticTokenProvider
    client = CCTradingClient(session_provider=StaticTokenProvider(token))

    # ── section 2: me() ─────────────────────────────────────────────────────
    account_id = probe_me(client)

    # ── section 3: checkListingStatus ───────────────────────────────────────
    listing_id = args.listing_id
    if not listing_id:
        INFO("No --listing-id given; auto-discovering from public marketplace…")
        listing_id = _get_listing_id_from_marketplace()
    probe_check_listing_status(client, listing_id)

    # ── section 4: account_listings ─────────────────────────────────────────
    probe_account_listings(client, account_id)

    # ── section 5: dry buy probe (explicit opt-in only) ─────────────────────
    if args.dry_buy_nft:
        if not args.dry_buy_price or not args.dry_buy_receipt:
            WARN("--dry-buy-nft given but --dry-buy-price / --dry-buy-receipt missing — skipping buy probe")
        else:
            probe_buy_shape(client, args.dry_buy_nft,
                            args.dry_buy_price, args.dry_buy_receipt)
    else:
        HEAD("5 / marketplace/buy  [SKIPPED — pass --dry-buy-nft to enable]")
        INFO("This probe calls marketplace/buy prepare (no signing, no broadcast).")
        INFO("Use Browser DevTools first to confirm the request shape, then run:")
        INFO("  python tools/probe_live.py --dry-buy-nft <addr> --dry-buy-price <p> --dry-buy-receipt <id>")

    # ── summary ─────────────────────────────────────────────────────────────
    write_results(args)


if __name__ == "__main__":
    main()
