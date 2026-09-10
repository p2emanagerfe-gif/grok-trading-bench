"""Desk-level wiring: the paths that only break when the pieces are combined."""

import json

import pytest
import yaml

from src.desk import TradingDesk
from src.models import Market, Position, Token
from tests.conftest import FakeClient, FakeResponse

GOOD = {
    "crypto_pulse": {"regime": "risk_on", "go_signal": 0.9, "risk_appetite": 0.8},
    "auditor": {"coordinated_buys": False, "wash_trading": False, "bundled_launch": False,
                "sniper_pct": 0.02, "insider_pct": 0.01, "safety_score": 0.95, "red_flags": []},
    "narrative": {"meme_score": 0.85, "originality": 0.8, "virality": 0.9,
                  "community_signal": 0.7, "is_derivative": False, "theme": "dog"},
    "crypto_checker": {"approve": True, "confidence": 0.8, "adjusted_score": 0.8,
                       "kill_reasons": []},
    "exit_manager": {"action": "HOLD", "reason": "intact", "confidence": 0.6},
}

TOKEN = Token(mint="M1", symbol="WIF2", holders=200, buys=90, sells=25, unique_traders=60,
              liquidity_usd=40000, mint_revoked=True, age_seconds=300)


def build(tmp_path, overrides=None, **kwargs) -> TradingDesk:
    with open("config.example.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    config["logging"] = {"path": str(tmp_path / "desk.jsonl"), "echo_stdout": False, "cost_report_every": 0}
    desk = TradingDesk(config, dry_run=kwargs.pop("dry_run", True), **kwargs)

    replies = {**GOOD, **(overrides or {})}
    for name, reply in replies.items():
        getattr(desk, name)._client = FakeClient([reply])
    return desk


async def test_a_clean_token_is_bought(tmp_path):
    desk = build(tmp_path)
    result = await desk.evaluate_token(TOKEN)
    assert result["bought"] is True
    records = [json.loads(line) for line in open(tmp_path / "desk.jsonl", encoding="utf-8")]
    assert records[-1]["type"] == "buy"
    assert records[-1]["market"] == "crypto"


async def test_a_wash_traded_token_never_reaches_the_checker(tmp_path):
    desk = build(tmp_path, {"auditor": {**GOOD["auditor"], "wash_trading": True}})
    result = await desk.evaluate_token(TOKEN)
    assert result["reason"] == "veto_wash_trading"
    assert desk.crypto_checker._client.calls == []


async def test_a_paused_crypto_market_blocks_everything(tmp_path):
    desk = build(tmp_path, {"crypto_pulse": {"regime": "risk_off", "go_signal": 0.1}})
    assert (await desk.evaluate_token(TOKEN))["reason"] == "veto_market_paused"


async def test_the_checker_can_veto_a_high_scoring_token(tmp_path):
    desk = build(tmp_path, {"crypto_checker": {"approve": False, "confidence": 0.9, "kill_reasons": ["thin"]}})
    assert (await desk.evaluate_token(TOKEN))["reason"] == "checker_rejected"


async def test_the_daily_loss_limit_stops_new_entries(tmp_path):
    desk = build(tmp_path)
    desk.risk.record_close(Market.CRYPTO, -desk.risk.daily_loss_limit_usd)
    assert (await desk.evaluate_token(TOKEN))["reason"] == "daily_loss_limit_reached"


async def test_allocation_is_crypto_only_and_logged(tmp_path):
    desk = build(tmp_path)
    allocation = await desk.run_allocation()
    assert allocation.crypto_pct == 1.0
    assert allocation.stocks_pct == 0.0
    kinds = [json.loads(line)["type"] for line in open(tmp_path / "desk.jsonl", encoding="utf-8")]
    assert "allocation" in kinds and "cost" in kinds


async def test_run_allocation_skips_the_allocator_model_call(tmp_path):
    desk = build(tmp_path)
    desk.allocator._client = FakeClient([{"crypto_pct": 0.2, "stocks_pct": 0.8, "reason": "ignored"}])
    await desk.run_allocation()
    assert desk.allocator._client.calls == []


async def test_exit_pass_holds_and_logs_an_action(tmp_path):
    desk = build(tmp_path)
    desk.positions = [Position(market=Market.CRYPTO, symbol="WIF2", quantity=1000,
                               entry_price=0.001, current_price=0.002)]
    decisions = await desk.run_exit_pass()
    assert decisions[0]["action"] == "HOLD"
    actions = [json.loads(l) for l in open(tmp_path / "desk.jsonl", encoding="utf-8") if '"action"' in l]
    assert actions[-1]["action"] == "HOLD"


async def test_refresh_positions_drops_non_crypto_entries(tmp_path):
    desk = build(tmp_path)
    desk.positions = [
        Position(market=Market.CRYPTO, symbol="WIF2", quantity=1, entry_price=1.0),
        Position(market=Market.STOCKS, symbol="ACME", quantity=1, entry_price=1.0),
    ]
    positions = await desk.refresh_positions()
    assert [p.market for p in positions] == [Market.CRYPTO]


async def test_manage_position_refuses_non_crypto_entries(tmp_path):
    desk = build(tmp_path)
    decision = await desk.manage_position(
        Position(market=Market.STOCKS, symbol="ACME", quantity=1, entry_price=1.0)
    )
    assert decision["reason"] == "market_disabled"
    records = [json.loads(line) for line in open(tmp_path / "desk.jsonl", encoding="utf-8")]
    assert records[-1]["reason"] == "market_disabled"


async def test_memory_reaches_the_checker_and_shared_agents(tmp_path):
    desk = build(tmp_path)
    assert desk.crypto_checker.memory is desk.memory
    assert desk.exit_manager.memory is desk.memory
    assert desk.allocator.memory is desk.memory
    assert desk.auditor.memory is None


async def test_past_outcomes_reach_the_checker_prompt(tmp_path):
    desk = build(tmp_path)
    desk.log.buy("crypto", "PUPPY", 0.7, {"narrative": {"theme": "dog"}}, 100.0, "tx")
    desk.log.close("crypto", "PUPPY", -60.0, 4.0)
    desk.refresh_memory()

    await desk.evaluate_token(TOKEN)
    sent = desk.crypto_checker._client.calls[0]["json"]["messages"][1]["content"]
    assert "past_outcomes" in sent and "PUPPY" in sent


async def test_costs_accumulate_across_the_whole_desk(tmp_path):
    desk = build(tmp_path)
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "cost_in_usd_ticks": 10_000_000_000}
    for name in ("crypto_pulse", "auditor", "narrative", "crypto_checker"):
        getattr(desk, name)._client = FakeClient([FakeResponse(json.dumps(GOOD[name]), usage=usage)])

    await desk.evaluate_token(TOKEN)
    snap = desk.costs.snapshot()
    assert snap["calls"] == 4
    assert snap["cost_usd"] == pytest.approx(4.0)
    assert set(snap["by_agent"]) == {"crypto_pulse", "auditor", "narrative", "crypto_checker"}


def test_shipped_config_uses_live_model_slugs(tmp_path):
    desk = build(tmp_path)
    assert desk.narrative.model == "grok-4.3"
    assert desk.crypto_checker.model == "grok-4.6"
    assert desk.crypto_checker.model != desk.narrative.model


def test_every_retrieval_agent_declares_a_search_policy(tmp_path):
    desk = build(tmp_path)
    for name in ("auditor", "narrative", "crypto_pulse", "crypto_checker", "exit_manager"):
        assert getattr(desk, name).SEARCH is not None, name
