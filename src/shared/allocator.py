"""Bot 10 — crypto-only allocator.

Runs once a day. The desk no longer trades stocks, so allocation is fixed at
100% crypto and 0% stocks.
"""

from __future__ import annotations

from typing import Any

from ..models import Allocation

_CRYPTO_ONLY = {"crypto_pct": 1.0, "stocks_pct": 0.0, "reason": "crypto_only_mode"}


class Allocator:
    name = "allocator"
    SEARCH = None

    def __init__(self, config: dict[str, Any], client=None, costs=None):
        self.config = config or {}
        self.client = client
        self.costs = costs
        self.memory = None

    def fixed_allocation(self) -> dict[str, Any]:
        return dict(_CRYPTO_ONLY)

    def allocate(self, risk: dict[str, Any] | None = None) -> Allocation:
        """Return the crypto-only allocation, clamped to the configured ceilings."""
        result = self.fixed_allocation()
        risk = risk or (self.config.get("risk", {}) or {})
        allocation = Allocation(
            crypto_pct=result["crypto_pct"],
            stocks_pct=result["stocks_pct"],
            reason=result.get("reason", ""),
        )
        return allocation.normalized(
            crypto_max_pct=float(risk.get("crypto_max_pct", 1.0)),
            stock_max_pct=float(risk.get("stock_max_pct", 1.0)),
        )
