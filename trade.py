"""Command-line entry point for the autonomous trader.

Runs a single decision cycle and prints a report. Dry-run by default — it
spends nothing unless TRADER_LIVE=true *and* the live executor is implemented.

Usage:
    python trade.py            # one dry-run cycle, human-readable report
    python trade.py --json     # same, machine-readable JSON
    python trade.py --loop 300 # repeat every 300 seconds
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from collectorcrypt.trader import TradeEngine, WalletError, load_config


def _print_human(report: dict) -> None:
    print(f"\n=== CC Trader [{report['mode']}] ===")
    print(f"Wallet            : {report['wallet']}")
    print(f"SOL rate          : ${report['sol_rate']:,.2f}")
    print(f"SOL balance       : {report['sol_balance']:.4f}")
    print(f"USDC balance      : {report['usdc_balance']:,.2f}")
    print(f"Available volume  : {report['available_volume']:,.2f} USDC")
    print(f"  ├ direct budget : {report['direct_budget']:,.2f} USDC")
    print(f"  └ offer budget  : {report['offer_budget']:,.2f} USDC")
    print(f"Per-card cap      : ${report['card_cap_usd']:,.2f}"
          f"{'  (ESCALATED)' if report['escalated'] else ''}")
    print(f"Scanned listings  : {report['scanned']}")
    print(f"Candidates (CC)   : {report['candidates']}")
    print(f"Planned buys      : {report['planned_buys']}"
          f"  (cost {report['planned_cost']:,.2f} USDC)")
    print(f"Planned offers    : {report['planned_offers']}"
          f"  (locked {report['planned_offer_cost']:,.2f} USDC)")
    print(f"Remaining volume  : {report['remaining_volume']:,.2f} USDC")
    print(f"Skipped           : {report['skipped']}")
    print(f"Fills ok          : {report['fills_ok']}")
    if report["items"]:
        print("\nDirect buys:")
        print("  #  ask      market   disc%  category      name")
        for i, it in enumerate(report["items"], 1):
            print(f"  {i:>2} {it['ask_usd']:>7.2f} {it['market_usd']:>8.2f} "
                  f"{it['discount_pct']:>5.1f}  {it['category'][:12]:<12} "
                  f"{it['name'][:48]}")
    if report["offers"]:
        print("\nOffers (bids below ask):")
        print("  #  ask      bid      market   category      name")
        for i, it in enumerate(report["offers"], 1):
            print(f"  {i:>2} {it['ask_usd']:>7.2f} {it['offer_usd']:>8.2f} "
                  f"{it['market_usd']:>8.2f}  {it['category'][:12]:<12} "
                  f"{it['name'][:48]}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CC autonomous trader")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON")
    parser.add_argument("--loop", type=int, metavar="SECONDS", default=0,
                        help="repeat every N seconds (0 = run once)")
    args = parser.parse_args(argv)

    cfg = load_config()
    try:
        engine = TradeEngine(cfg)
    except WalletError as exc:
        print(f"Wallet error: {exc}", file=sys.stderr)
        return 2

    while True:
        try:
            report = engine.run_cycle()
        except WalletError as exc:
            print(f"Wallet error: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - top-level guard
            print(f"Cycle error: {exc}", file=sys.stderr)
            if args.loop <= 0:
                return 1
            time.sleep(args.loop)
            continue

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_human(report)

        if args.loop <= 0:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
