from datetime import date, timedelta

import pytest

from src.models import Allocation, Market, Position
from src.shared.risk import RiskManager

CONFIG = {
    "risk": {
        "total_budget_usd": 2000.0,
        "daily_loss_limit_usd": 300.0,
        "max_open_total": 5,
        "max_open_crypto": 3,
        "crypto_max_pct": 1.0,
        "stock_max_pct": 0.0,
        "max_position_pct_of_market": 0.15,
        "max_position_pct_of_remaining_loss": 0.25,
    }
}


def rm() -> RiskManager:
    return RiskManager(CONFIG)


def pos(symbol="X") -> Position:
    return Position(market=Market.CRYPTO, symbol=symbol, quantity=1, entry_price=1.0)


def test_default_allocation_is_crypto_only():
    r = rm()
    assert r.market_budget(Market.CRYPTO) == 2000.0
    assert r.market_budget(Market.STOCKS) == 0.0


def test_allocation_is_clamped_to_crypto_only():
    r = rm()
    r.set_allocation(Allocation(crypto_pct=0.2, stocks_pct=0.8))
    assert r.allocation.crypto_pct == 1.0
    assert r.allocation.stocks_pct == 0.0


def test_stock_budget_stays_disabled_even_if_config_requests_it():
    r = RiskManager({"risk": {**CONFIG["risk"], "stock_max_pct": 0.9}})
    r.set_allocation(Allocation(crypto_pct=0.2, stocks_pct=0.8))
    assert r.market_budget(Market.CRYPTO) == 2000.0
    assert r.market_budget(Market.STOCKS) == 0.0


def test_open_is_allowed_on_an_empty_crypto_book():
    assert rm().can_open(Market.CRYPTO, []) == (True, "ok")


def test_stock_market_is_disabled():
    assert rm().can_open(Market.STOCKS, []) == (False, "market_disabled")


def test_max_open_total_counts_existing_positions():
    r = rm()
    positions = [pos(str(i)) for i in range(5)]
    assert r.can_open(Market.CRYPTO, positions) == (False, "max_open_total")


def test_max_open_crypto_is_enforced():
    r = rm()
    positions = [pos(str(i)) for i in range(3)]
    assert r.can_open(Market.CRYPTO, positions) == (False, "max_open_crypto")


def test_daily_loss_limit_shuts_new_crypto_entries():
    r = rm()
    r.record_close(Market.CRYPTO, -300.0)
    assert r.daily_loss_breached() is True
    assert r.can_open(Market.CRYPTO, []) == (False, "daily_loss_limit_reached")


def test_profits_do_not_inflate_the_loss_room():
    r = rm()
    r.record_close(Market.CRYPTO, 500.0)
    assert r.remaining_loss_room() == pytest.approx(300.0)


def test_daily_reset_clears_pnl_and_deployment():
    r = rm()
    r.record_close(Market.CRYPTO, -290.0)
    r.record_fill(Market.CRYPTO, 400.0)
    assert r.maybe_reset_day(r.session_date) is False
    assert r.maybe_reset_day(date.today() + timedelta(days=1)) is True
    assert r.realized_pnl_today == 0.0
    assert r.deployed_usd[Market.CRYPTO] == 0.0


def test_deployed_capital_exhausts_the_market_budget():
    r = rm()
    r.record_fill(Market.CRYPTO, 2000.0)
    assert r.remaining_market_budget(Market.CRYPTO) == 0.0
    assert r.can_open(Market.CRYPTO, []) == (False, "market_budget_exhausted")


def test_an_oversized_order_is_rejected():
    r = rm()
    r.record_fill(Market.CRYPTO, 1900.0)
    assert r.can_open(Market.CRYPTO, [], amount_usd=50.0) == (True, "ok")
    assert r.can_open(Market.CRYPTO, [], amount_usd=150.0) == (False, "exceeds_market_budget")


def test_close_returns_budget_and_records_pnl():
    r = rm()
    r.record_fill(Market.CRYPTO, 500.0)
    r.record_close(Market.CRYPTO, 40.0, amount_usd=500.0)
    assert r.deployed_usd[Market.CRYPTO] == 0.0
    assert r.realized_pnl_today == pytest.approx(40.0)


def test_position_size_respects_the_market_cap():
    assert rm().position_size(Market.CRYPTO, score=1.0) == 75.0


def test_position_size_is_bound_by_remaining_loss_room():
    r = rm()
    r.record_close(Market.CRYPTO, -200.0)
    assert r.position_size(Market.CRYPTO, score=1.0) == 25.0


def test_position_size_is_bound_by_free_budget():
    r = rm()
    r.record_fill(Market.CRYPTO, 1990.0)
    assert r.position_size(Market.CRYPTO, score=1.0) == 10.0


def test_position_size_scales_with_score():
    r = rm()
    full = r.position_size(Market.CRYPTO, score=1.0)
    assert r.position_size(Market.CRYPTO, score=0.0) == pytest.approx(full * 0.5)
    assert r.position_size(Market.CRYPTO, score=0.5) == pytest.approx(full * 0.75)


def test_position_size_is_zero_when_the_day_is_blown():
    r = rm()
    r.record_close(Market.CRYPTO, -400.0)
    assert r.position_size(Market.CRYPTO, score=1.0) == 0.0


def test_snapshot_reports_the_crypto_book():
    r = rm()
    r.record_fill(Market.CRYPTO, 100.0)
    snap = r.snapshot([pos()])
    assert snap["open_positions"] == {"total": 1, "crypto": 1, "stocks": 0}
    assert snap["deployed"]["crypto"] == 100.0
    assert snap["budgets"]["stocks"] == 0.0
