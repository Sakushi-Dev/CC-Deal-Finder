"""Order execution.

Two executors share one interface:

* :class:`DryRunExecutor` — simulates fills, spends nothing, touches no key.
  This is the default and what we run until the full live flow is verified.
* :class:`LiveExecutor` — the only component that would ever spend real funds.
  It is intentionally **not implemented yet**: the CollectorCrypt buy flow
  (Privy SIWS auth -> ``marketplace/buy`` -> local sign -> ``marketplace/broadcast``)
  is undocumented and must be reverse-engineered and tested against a funded
  test wallet before it is enabled. Any attempt to run it raises, so live
  spending cannot happen by accident.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .strategy import BuyPlan, Candidate, Offer


@dataclass
class Fill:
    """The result of attempting a single action (buy or offer)."""

    candidate: Candidate
    ok: bool
    simulated: bool
    kind: str = "buy"  # "buy" or "offer"
    price_usd: float = 0.0
    detail: str = ""
    signature: str = ""


class Executor(Protocol):
    def execute(self, plan: BuyPlan) -> list[Fill]: ...


class DryRunExecutor:
    """Pretends to buy / place offers for everything in the plan. Spends nothing."""

    def execute(self, plan: BuyPlan) -> list[Fill]:
        fills: list[Fill] = []
        for item in plan.items:
            fills.append(
                Fill(candidate=item, ok=True, simulated=True, kind="buy",
                     price_usd=item.ask_usd, detail="dry-run: no transaction sent")
            )
            # Sell rule: immediately relist the bought card below market value.
            if item.resell_usd > 0:
                fills.append(
                    Fill(candidate=item, ok=True, simulated=True, kind="list",
                         price_usd=item.resell_usd,
                         detail="dry-run: would relist for sale")
                )
        fills.extend(
            Fill(candidate=offer.candidate, ok=True, simulated=True, kind="offer",
                 price_usd=offer.offer_usd, detail="dry-run: no offer sent")
            for offer in plan.offers
        )
        return fills


class LiveExecutor:
    """Real on-chain purchases. Deliberately disabled until verified."""

    def __init__(self, wallet, rpc_url: str) -> None:
        self._wallet = wallet
        self._rpc_url = rpc_url

    def execute(self, plan: BuyPlan) -> list[Fill]:  # pragma: no cover
        raise NotImplementedError(
            "Live execution is not implemented yet. The CollectorCrypt buy "
            "and make-offer flows (auth -> marketplace/buy or "
            "marketplace/make-offer -> sign -> marketplace/broadcast) must be "
            "built and tested on a funded test wallet first. Keep "
            "TRADER_LIVE=false until then."
        )
