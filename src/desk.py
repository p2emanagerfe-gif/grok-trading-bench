"""Grok Trading Desk — the crypto-only orchestrator.

Three concurrent asyncio loops, one shared risk manager, one event log:

  crypto_loop     continuous, 24/7, driven by the pump.fun WebSocket
  exit_loop       every 4 hours over every open position
  allocator_loop  once a day, reaffirms the crypto-only budget split

Run:  python -m src.desk --config config.yaml [--dry-run] [--i-understand-the-risk]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .base_agent import CostTracker
from .crypto.auditor import Auditor
from .crypto.crypto_checker import CryptoChecker
from .crypto.crypto_executor import CryptoExecutor
from .crypto.crypto_pulse import CryptoPulse
from .crypto.crypto_scoring import score_token
from .crypto.narrative import Narrative
from .crypto.scout import Scout
from .models import Allocation, Market, Position
from .shared.allocator import Allocator
from .shared.exit_manager import ExitManager
from .shared.log import EventLog
from .shared.memory import OutcomeMemory
from .shared.risk import RiskManager

log = logging.getLogger("desk")


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class TradingDesk:
    """Owns every bot, the shared risk state and the crypto-only loops."""

    def __init__(self, config: dict[str, Any], dry_run: bool = False, live_ack: bool = False):
        self.config = config
        self.dry_run = dry_run

        self.log = EventLog(config)
        self.risk = RiskManager(config)
        # One tracker across every bot, so spend is a desk number not a per-bot one.
        self.costs = CostTracker()
        self.memory = OutcomeMemory(config, event_log=self.log)

        def agent(cls):
            return cls(config, costs=self.costs)

        # crypto side
        self.scout = Scout(config)
        self.auditor = agent(Auditor)
        self.narrative = agent(Narrative)
        self.crypto_pulse = agent(CryptoPulse)
        self.crypto_checker = agent(CryptoChecker)
        self.crypto_executor = CryptoExecutor(config)

        # shared
        self.allocator = agent(Allocator)
        self.exit_manager = agent(ExitManager)

        # Only the agents that decide get history; the analysts describe what is
        # in front of them and should not be anchored by old trades.
        for bot in (self.crypto_checker, self.exit_manager, self.allocator):
            bot.memory = self.memory

        self.positions: list[Position] = []
        self.min_go_signal = float((config.get("pulse", {}) or {}).get("min_go_signal", 0.3))
        self.weights = config.get("scoring_weights", {}) or {}
        self.exits_cfg = config.get("exits", {}) or {}
        self._lock = asyncio.Lock()
        self._cost_report_every = int(
            (config.get("logging", {}) or {}).get("cost_report_every", 25)
        )
        self._last_cost_report = 0

    # -- helpers -------------------------------------------------------------------

    def maybe_report_costs(self) -> None:
        """Emit a spend snapshot every N model calls."""
        if self._cost_report_every <= 0:
            return
        if self.costs.calls - self._last_cost_report < self._cost_report_every:
            return
        self._last_cost_report = self.costs.calls
        self.log.write("cost", **self.costs.snapshot())

    def refresh_memory(self) -> int:
        """Re-read the log so the next decision sees the latest outcomes."""
        try:
            return len(self.memory.load())
        except Exception as exc:  # noqa: BLE001 - memory is an enhancement, not a gate
            log.warning("could not refresh outcome memory: %s", exc)
            return 0

    # -- crypto loop ----------------------------------------------------------------

    async def evaluate_token(self, token) -> dict[str, Any]:
        """Audit + narrative + pulse -> score -> adversarial check -> buy or skip."""
        pulse, audit, narrative = await asyncio.gather(
            self.crypto_pulse.run(),
            self.auditor.run(token),
            self.narrative.run(token),
        )

        verdict = score_token(
            token,
            audit,
            narrative,
            pulse,
            weights=self.weights.get("crypto"),
            min_go_signal=self.min_go_signal,
        )
        agent_scores = {
            "audit": audit, "narrative": narrative, "pulse": pulse, "matrix": verdict,
            "citations": self.auditor.last_citations + self.narrative.last_citations,
        }
        self.maybe_report_costs()

        if not verdict["buy"]:
            self.log.skip(Market.CRYPTO.value, token.symbol or token.mint, verdict["reason"],
                          {"score": verdict["score"]})
            return {"bought": False, "reason": verdict["reason"]}

        check = await self.crypto_checker.run(
            {"token": token.model_dump(mode="json"), "audit": audit,
             "narrative": narrative, "pulse": pulse, "score": verdict}
        )
        agent_scores["checker"] = check
        agent_scores["citations"] += self.crypto_checker.last_citations
        self.maybe_report_costs()
        if not check["approve"]:
            self.log.skip(Market.CRYPTO.value, token.symbol or token.mint, "checker_rejected",
                          {"kill_reasons": check["kill_reasons"]})
            return {"bought": False, "reason": "checker_rejected"}

        return await self._open_crypto(token, verdict, check, agent_scores)

    async def _open_crypto(self, token, verdict, check, agent_scores) -> dict[str, Any]:
        async with self._lock:
            allowed, reason = self.risk.can_open(Market.CRYPTO, self.positions)
            if not allowed:
                self.log.skip(Market.CRYPTO.value, token.symbol or token.mint, reason)
                return {"bought": False, "reason": reason}

            amount = self.risk.position_size(Market.CRYPTO, score=check["adjusted_score"] or verdict["score"])
            if amount <= 0:
                self.log.skip(Market.CRYPTO.value, token.symbol or token.mint, "size_zero")
                return {"bought": False, "reason": "size_zero"}

            if self.dry_run:
                self.log.buy(Market.CRYPTO.value, token.symbol or token.mint, verdict["score"],
                             agent_scores, amount, tx_id="DRY_RUN")
                return {"bought": True, "dry_run": True, "amount": amount}

            try:
                fill = await self.crypto_executor.buy(token.mint, amount)
            except NotImplementedError as exc:
                # the stub is expected until the owner wires signing
                self.log.skip(Market.CRYPTO.value, token.symbol or token.mint,
                              "executor_not_implemented", str(exc))
                return {"bought": False, "reason": "executor_not_implemented"}

            self.risk.record_fill(Market.CRYPTO, amount)
            self.positions.append(
                Position(
                    market=Market.CRYPTO,
                    symbol=token.symbol or token.mint,
                    quantity=float(fill.get("quantity", 0)),
                    entry_price=float(fill.get("price", 0)),
                    amount_usd=amount,
                    score=verdict["score"],
                    meta={"mint": token.mint, "tx_id": fill.get("tx_id", "")},
                )
            )
            self.log.buy(Market.CRYPTO.value, token.symbol or token.mint, verdict["score"],
                         agent_scores, amount, tx_id=str(fill.get("tx_id", "")))
            return {"bought": True, "amount": amount, "tx_id": fill.get("tx_id", "")}

    async def crypto_loop(self) -> None:
        log.info("crypto loop: streaming pump.fun")
        while True:
            try:
                async for token in self.scout.stream():
                    self.risk.maybe_reset_day()
                    await self.evaluate_token(token)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a loop must not die on one bad token
                log.exception("crypto loop error, restarting in 10s")
                await asyncio.sleep(10)

    # -- exit loop --------------------------------------------------------------------

    async def refresh_positions(self) -> list[Position]:
        """Crypto positions remain desk-side while execution is a stub."""
        self.positions = [p for p in self.positions if p.market == Market.CRYPTO]
        return self.positions

    async def manage_position(self, position: Position) -> dict[str, Any]:
        if position.market != Market.CRYPTO:
            self.log.skip(position.market.value, position.symbol, "market_disabled")
            return {"action": "HOLD", "reason": "market_disabled", "confidence": 0.0}

        decision = await self.exit_manager.run(position)
        action = decision["action"]
        self.log.action(position.symbol, action, decision["reason"],
                        market=position.market.value, pnl_pct=round(position.pnl_pct, 4))

        if action == "HOLD" or self.dry_run:
            return decision

        executor = self.crypto_executor
        try:
            if action == "TIGHTEN":
                new_stop = position.current_price * (1 - decision["new_stop_pct"])
                await executor.tighten_stop(position.meta.get("mint", ""), new_stop)
                position.stop_price = new_stop

            elif action == "TRIM":
                fraction = decision["trim_fraction"]
                await executor.sell(position.meta.get("mint", ""), fraction)
                position.quantity *= 1 - fraction
                position.amount_usd *= 1 - fraction

            elif action == "CLOSE":
                await executor.close_position(position.meta.get("mint", ""))
                self.risk.record_close(position.market, position.pnl_usd, position.amount_usd)
                self.log.close(position.market.value, position.symbol,
                               round(position.pnl_usd, 2), round(position.hold_time_hours, 2))
                self.positions = [p for p in self.positions if p is not position]

        except NotImplementedError as exc:
            self.log.skip(position.market.value, position.symbol,
                          "executor_not_implemented", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to %s %s", action, position.symbol)
            self.log.skip(position.market.value, position.symbol, "action_failed", str(exc))

        return decision

    async def run_exit_pass(self) -> list[dict[str, Any]]:
        self.refresh_memory()
        positions = await self.refresh_positions()
        log.info("exit pass over %d positions", len(positions))
        return [await self.manage_position(p) for p in list(positions)]

    async def exit_loop(self) -> None:
        interval = float(self.exits_cfg.get("interval_hours", 4)) * 3600
        log.info("exit loop: every %.1f h", interval / 3600)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_exit_pass()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("exit loop error")

    # -- allocator loop -----------------------------------------------------------------

    def weekly_pnl(self) -> dict[str, float]:
        """Realised crypto PnL over the trailing seven days, from the log."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        totals = {"crypto": 0.0}
        for record in self.log.read():
            if record.get("type") != "close":
                continue
            try:
                when = datetime.fromisoformat(record["ts"])
            except (KeyError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < cutoff:
                continue
            market = record.get("market", "")
            if market in totals:
                totals[market] += float(record.get("pnl", 0) or 0)
        return totals

    async def run_allocation(self) -> Allocation:
        self.refresh_memory()
        crypto_pulse = await self.crypto_pulse.run()
        allocation = await self.allocator.allocate(
            crypto_pulse, None, self.weekly_pnl(), risk=self.config.get("risk")
        )
        applied = self.risk.set_allocation(allocation)
        self.log.allocation(round(applied.crypto_pct, 4), round(applied.stocks_pct, 4),
                            allocation.reason)
        self.log.write("cost", **self.costs.snapshot())
        return applied

    async def allocator_loop(self, interval_seconds: float = 86400.0) -> None:
        log.info("allocator loop: daily")
        while True:
            try:
                await self.run_allocation()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("allocator loop error")
            await asyncio.sleep(interval_seconds)

    # -- entry point ---------------------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "desk starting — dry_run=%s, crypto-only, models=%s/%s, live_search=%s",
            self.dry_run,
            self.narrative.model,
            self.crypto_checker.model,
            self.narrative.live_search,
        )
        log.info("outcome memory: %d closed trades loaded", self.refresh_memory())
        await asyncio.gather(
            self.crypto_loop(),
            self.exit_loop(),
            self.allocator_loop(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Grok Trading Desk")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="decide and log, never execute")
    parser.add_argument(
        "--i-understand-the-risk",
        action="store_true",
        dest="live_ack",
        help='required, together with mode: "live" in the config, to leave paper trading',
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    desk = TradingDesk(load_config(args.config), dry_run=args.dry_run, live_ack=args.live_ack)
    try:
        asyncio.run(desk.run())
    except KeyboardInterrupt:
        log.info("desk stopped")


if __name__ == "__main__":
    main()
