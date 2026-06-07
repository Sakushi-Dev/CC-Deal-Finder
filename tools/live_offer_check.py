"""Controlled, two-phase live verification of the reversible escrow-offer path.

This is the §4 live-readiness check from ``docs/live-readiness-plan.md``: the one
**reversible** real write that proves the whole signing + broadcast lifecycle
end-to-end on-chain. An offer sits in escrow and is refundable via cancel, so —
unlike a buy, which settles instantly — it can be placed and then fully unwound.

It deliberately drives the **real** trading code, not a re-implementation: the
request bytes come from :class:`~collectorcrypt.trader.ccapi.CCTradingClient`
(``make_offer`` / ``update_offer`` / ``cancel_offer`` / ``broadcast``) and the
signature from :meth:`~collectorcrypt.trader.wallet.Wallet.sign_transaction` —
exactly what :class:`~collectorcrypt.trader.executor.LiveExecutor` sends. So a
green run here proves the same bytes the engine would emit.

Phases (``--phase``)
--------------------
* ``create`` (default, **moves no funds**): SIWS login, build the unsigned
  make-offer transaction and inspect its shape. It never signs and never
  broadcasts — nothing settles on-chain. Use this first to confirm the offer
  body is accepted (HTTP 2xx) and a transaction comes back.
* ``place`` (**moves USDC into escrow**): create -> sign locally -> broadcast.
  Requires ``--confirm-funds``. Funds are recoverable via ``--phase cancel``.
* ``bump`` (**re-prices the escrowed offer**): build update-offer -> sign ->
  broadcast at a higher ``--price``. Requires ``--confirm-funds``.
* ``cancel`` (**refunds the escrow**): build cancel-offer -> sign -> broadcast.
  Requires ``--confirm-funds``. This is the unwind step.

Safety
------
* Any fund-moving phase (place/bump/cancel) refuses to run without the explicit
  ``--confirm-funds`` flag, and prints the wallet's USDC/SOL balance before and
  after so the escrow movement is visible.
* The signed transaction, the broadcast signature and the bearer token are
  redacted from all output. The private key never leaves the process.
* Use a dedicated, minimally-funded test wallet (see the plan, §1). Keep the
  offer price tiny (a few USDC).

Usage (PowerShell, from the repo root with the venv active)::

    # 1) shape-only, no funds move (auto-pick the cheapest listed card):
    & ".venv\\Scripts\\python.exe" tools/live_offer_check.py --phase create

    # target a specific card and bid:
    & ".venv\\Scripts\\python.exe" tools/live_offer_check.py --phase create \
        --nft <nftAddress> --card-id <ccCardId> --price 3

    # 2) place the escrow offer for real (USDC -> escrow):
    & ".venv\\Scripts\\python.exe" tools/live_offer_check.py --phase place \
        --nft <nftAddress> --card-id <ccCardId> --price 3 --confirm-funds

    # 3) (optional) bump it, then 4) cancel to refund:
    & ".venv\\Scripts\\python.exe" tools/live_offer_check.py --phase bump \
        --nft <nftAddress> --price 4 --confirm-funds
    & ".venv\\Scripts\\python.exe" tools/live_offer_check.py --phase cancel \
        --nft <nftAddress> --confirm-funds

Requires ``TRADER_WALLET_SECRET`` (to sign) in the environment / ``.env``. Uses
the configured auth provider (``TRADER_AUTH_PROVIDER``); if that is ``none`` it
falls back to a direct Privy SIWS handshake, since a signing wallet is present.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectorcrypt import config as app_config  # noqa: E402
from collectorcrypt.api import CCClient  # noqa: E402
from collectorcrypt.trader import config as trader_config  # noqa: E402
from collectorcrypt.trader.auth import (  # noqa: E402
    NullSessionProvider, SessionProvider,
)
from collectorcrypt.trader.ccapi import CCApiError, CCTradingClient  # noqa: E402
from collectorcrypt.trader.executor import (  # noqa: E402
    _extract_signature, _extract_tx,
)
from collectorcrypt.trader.siws import (  # noqa: E402
    PrivySiwsProvider, make_session_provider,
)
from collectorcrypt.trader.wallet import Wallet, WalletError  # noqa: E402

_FUND_MOVING = ("place", "bump", "cancel")


# --------------------------------------------------------------------------- #
# Output helpers (secrets redacted)
# --------------------------------------------------------------------------- #
def info(text: str) -> None:
    print(f"  {text}")


def ok(text: str) -> None:
    print(f"  [OK]   {text}")


def warn(text: str) -> None:
    print(f"  [WARN] {text}")


def fail(text: str) -> None:
    print(f"  [FAIL] {text}")


def header(text: str) -> None:
    print(f"\n=== {text} ===")


def _short(secret: str) -> str:
    """A redacted fingerprint of a secret-bearing string (tx / signature)."""
    if not secret:
        return "<empty>"
    return f"<len={len(secret)} {secret[:6]}…{secret[-4:]}>"


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
def _build_wallet(cfg: Any) -> Wallet:
    wallet = Wallet(cfg.rpc_url, address=cfg.wallet_address,
                    secret=cfg.wallet_secret)
    if not wallet.can_sign:
        raise WalletError(
            "No signing wallet: set TRADER_WALLET_SECRET in the environment / "
            ".env. A read-only wallet cannot place or cancel an offer."
        )
    return wallet


def _build_session_provider(cfg: Any, wallet: Wallet) -> SessionProvider:
    """Resolve a real auth provider, mirroring the engine's selection.

    Uses the configured provider; if that is the null provider (TRADER_AUTH_
    PROVIDER unset) we fall back to a direct Privy SIWS handshake, since a
    signing wallet is available — exactly what the live engine would do once
    armed.
    """
    provider = make_session_provider(cfg, wallet)
    if isinstance(provider, NullSessionProvider):
        info("TRADER_AUTH_PROVIDER is 'none' -> falling back to direct Privy "
             "SIWS (a signing wallet is present).")
        provider = PrivySiwsProvider(
            wallet, app_id=cfg.privy_app_id, client_id=cfg.privy_client_id)
    return provider


def _discover_cheapest_card() -> tuple[str, str, float]:
    """Return (nftAddress, ccCardId, askPrice) for the cheapest listed card.

    Best-effort discovery from the public marketplace so ``--phase create`` can
    run without hand-picking a card. Returns empty strings on failure.
    """
    try:
        page = CCClient().fetch_marketplace_page(1, 60)
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        warn(f"could not auto-discover a card: {exc}")
        return "", "", 0.0
    best: tuple[str, str, float] | None = None
    for card in page.get("filterNFtCard", []) or []:
        listing = card.get("listing") or {}
        nft = str(card.get("nftAddress") or "")
        card_id = str(card.get("id") or "")
        price = float(listing.get("price") or 0.0)
        if not (nft and card_id and price > 0):
            continue
        if best is None or price < best[2]:
            best = (nft, card_id, price)
    if best is None:
        warn("no usable listed card found on marketplace page 1.")
        return "", "", 0.0
    return best


def _balances(wallet: Wallet) -> tuple[float, float]:
    """(usdc, sol) balances; 0.0 on RPC failure (printed, never fatal)."""
    try:
        usdc = wallet.usdc_balance()
    except WalletError as exc:
        warn(f"could not read USDC balance: {exc}")
        usdc = 0.0
    try:
        sol = wallet.sol_balance()
    except WalletError as exc:
        warn(f"could not read SOL balance: {exc}")
        sol = 0.0
    return usdc, sol


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
def _build_offer_tx(client: CCTradingClient, phase: str, *, nft: str,
                    card_id: str, price: float, wallet_addr: str,
                    currency: str) -> str:
    """Call the real ccapi builder for ``phase`` and return the unsigned tx.

    These are the exact request bytes the LiveExecutor emits. No signing here.
    """
    if phase in ("create", "place"):
        info(f"POST marketplace/make-offer  "
             f"{{cardId, currency={currency}, nftAddress, price={price}, wallet}}")
        resp = client.make_offer(nft=nft, card_id=card_id, price=price,
                                 wallet=wallet_addr, currency=currency)
    elif phase == "bump":
        info(f"POST marketplace/update-offer  "
             f"{{buyer, currency={currency}, nftAddress, price={price}, wallet}}")
        resp = client.update_offer(nft=nft, price=price, wallet=wallet_addr,
                                   currency=currency)
    elif phase == "cancel":
        info(f"POST marketplace/cancel-offer  "
             f"{{coin={currency}, keepInEscrow=false, nftAddress, wallet}}")
        resp = client.cancel_offer(nft=nft, wallet=wallet_addr,
                                   currency=currency)
    else:  # pragma: no cover - argparse restricts choices
        raise ValueError(f"unknown phase {phase!r}")
    tx = _extract_tx(resp)
    if tx:
        ok(f"server returned an unsigned transaction: {_short(tx)}")
    else:
        fail("no transaction in the response — the request body shape may be "
             "wrong, or the offer does not exist (for bump/cancel).")
    return tx


def _sign_and_broadcast(wallet: Wallet, client: CCTradingClient, tx: str, *,
                        nft: str) -> str:
    """Sign ``tx`` locally and broadcast it. Returns the on-chain signature.

    Same two calls the LiveExecutor makes: ``wallet.sign_transaction`` then
    ``client.broadcast``. This is the step that actually settles on-chain.
    """
    try:
        signed = wallet.sign_transaction(tx)
    except WalletError as exc:
        fail(f"local signing failed: {exc}")
        return ""
    ok(f"signed locally (key never left the process): {_short(signed)}")
    try:
        resp = client.broadcast(signed_tx=signed, wallet=wallet.address, nft=nft)
    except CCApiError as exc:
        fail(f"broadcast failed: {type(exc).__name__}: {exc}")
        return ""
    signature = _extract_signature(resp)
    if signature:
        ok(f"broadcast accepted; on-chain signature: {_short(signature)}")
    else:
        warn("broadcast returned no signature field — inspect the response "
             "envelope (success flag may still indicate acceptance).")
    return signature


def run(args: argparse.Namespace) -> int:
    header("Environment")
    cfg = trader_config.load_config()
    info(f"API base: {app_config.API_BASE}")
    info(f"RPC: {cfg.rpc_url}")
    info(f"auth_provider: {cfg.auth_provider!r}  live: {cfg.live}")
    info(f"phase: {args.phase}")

    if cfg.rpc_url == trader_config.DEFAULT_RPC_URL:
        warn("Using the public Solana RPC (rate-limited). Set TRADER_RPC_URL to "
             "a dedicated endpoint for a reliable broadcast.")

    # Wallet (must be able to sign).
    try:
        wallet = _build_wallet(cfg)
    except WalletError as exc:
        fail(str(exc))
        return 1
    ok(f"signing wallet ready (address len={len(wallet.address)}).")

    # Resolve the target card.
    nft, card_id, price, currency = args.nft, args.card_id, args.price, args.currency
    if args.phase in ("create", "place") and not (nft and card_id):
        info("no --nft/--card-id given; auto-discovering the cheapest listed "
             "card for the offer target...")
        d_nft, d_card_id, d_ask = _discover_cheapest_card()
        nft = nft or d_nft
        card_id = card_id or d_card_id
        if d_ask and price <= 0:
            # Default to a small bid well under the ask for a shape-only run.
            price = round(min(d_ask * 0.5, 3.0), 2)
            info(f"discovered ask {d_ask}; defaulting offer price to {price}.")
    if not nft:
        fail("no target nftAddress (pass --nft, or let create auto-discover).")
        return 1
    if args.phase in ("create", "place") and not card_id:
        fail("make-offer requires the card's internal CC id (pass --card-id).")
        return 1
    if args.phase in _FUND_MOVING and price <= 0 and args.phase != "cancel":
        fail(f"--price must be > 0 for phase {args.phase}.")
        return 1

    info(f"target nft len={len(nft)}  card_id={card_id or '<n/a>'}  "
         f"price={price}  currency={currency}")

    # Fund-moving guard.
    moves_funds = args.phase in _FUND_MOVING
    if moves_funds and not args.confirm_funds:
        header("Refusing to move funds")
        fail(f"Phase '{args.phase}' signs and broadcasts a real transaction "
             "that moves funds on-chain. Re-run with --confirm-funds once you "
             "have reviewed the target and price above.")
        info("Tip: run '--phase create' first to inspect the unsigned tx "
             "without moving anything.")
        return 2

    # Build the authenticated client (establishes a session lazily on first call).
    try:
        provider = _build_session_provider(cfg, wallet)
    except CCApiError as exc:
        fail(f"could not build an auth provider: {exc}")
        return 1
    client = CCTradingClient(session_provider=provider)

    # Pre-balances for fund-moving phases.
    pre_usdc = pre_sol = 0.0
    if moves_funds:
        header("Balances before")
        pre_usdc, pre_sol = _balances(wallet)
        info(f"USDC: {pre_usdc:.4f}   SOL: {pre_sol:.6f}")

    # Phase 1: build the unsigned tx (real request bytes).
    header(f"Build unsigned transaction ({args.phase})")
    try:
        tx = _build_offer_tx(client, args.phase, nft=nft, card_id=card_id,
                             price=price, wallet_addr=wallet.address,
                             currency=currency)
    except CCApiError as exc:
        fail(f"request failed: {type(exc).__name__}: {exc}")
        info("A 4xx usually means a body field is wrong for this account/card "
             "state; compare against the DevTools capture in docs/api.md.")
        return 1
    if not tx:
        return 1

    if args.phase == "create":
        header("Done (create) — nothing signed, nothing broadcast")
        ok("The offer body was accepted and a signable transaction was "
           "returned. Re-run with '--phase place --confirm-funds' to settle it "
           "on-chain (USDC moves into escrow; refundable via '--phase cancel').")
        return 0

    # Phase 2: sign + broadcast (settles on-chain).
    header(f"Sign + broadcast ({args.phase})")
    signature = _sign_and_broadcast(wallet, client, tx, nft=nft)
    if not signature:
        warn("No confirmed signature. Check the wallet, RPC and the broadcast "
             "response above before retrying.")

    # Post-balances.
    header("Balances after")
    post_usdc, post_sol = _balances(wallet)
    info(f"USDC: {post_usdc:.4f}   SOL: {post_sol:.6f}")
    d_usdc, d_sol = post_usdc - pre_usdc, post_sol - pre_sol
    info(f"delta USDC: {d_usdc:+.4f}   delta SOL: {d_sol:+.6f}")
    if args.phase == "place" and d_usdc < 0:
        ok("USDC decreased — consistent with funds moving into escrow.")
    elif args.phase == "cancel" and d_usdc > 0:
        ok("USDC increased — consistent with the escrow being refunded.")
    else:
        info("Confirm the escrow movement in the CC UI / on-chain explorer "
             "(balances can lag until the tx finalizes).")

    header("Next")
    if args.phase == "place":
        info("Verify the offer is OPEN in the CC UI, then unwind it with "
             "'--phase cancel --confirm-funds' and confirm the refund.")
    elif args.phase == "bump":
        info("Confirm the offer now shows the higher price, then cancel to "
             "refund.")
    else:  # cancel
        ok("Reversible escrow round-trip complete once the refund is confirmed. "
           "This proves sign + broadcast end-to-end (plan §4).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Two-phase live verification of the reversible escrow-offer "
                    "path (docs/live-readiness-plan.md §4).")
    ap.add_argument("--phase", choices=("create", "place", "bump", "cancel"),
                    default="create",
                    help="create=build+inspect only (no funds); place/bump/"
                         "cancel sign+broadcast (need --confirm-funds).")
    ap.add_argument("--nft", default="",
                    help="Target card nftAddress (auto-discovered for create/"
                         "place if omitted).")
    ap.add_argument("--card-id", default="",
                    help="The card's internal CC id (raw card 'id'); required "
                         "by make-offer (create/place).")
    ap.add_argument("--price", type=float, default=0.0,
                    help="Offer price in the currency unit (USDC). Required for "
                         "place/bump.")
    ap.add_argument("--currency", default="USDC",
                    help="Offer currency (default USDC).")
    ap.add_argument("--confirm-funds", action="store_true",
                    help="Required acknowledgement for any phase that signs and "
                         "broadcasts a real, fund-moving transaction.")
    args = ap.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
