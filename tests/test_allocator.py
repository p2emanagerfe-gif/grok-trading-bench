from src.models import Allocation
from src.shared.allocator import Allocator
from tests.conftest import CONFIG

CRYPTO_PULSE = {"regime": "risk_on", "go_signal": 0.8}
PNL = {"crypto": 120.0}


def test_allocator_helper_is_hardwired_to_crypto_only(client_factory):
    client = client_factory({"crypto_pct": 0.2, "stocks_pct": 0.8})
    result = Allocator(CONFIG, client=client).fixed_allocation()
    assert result == {"crypto_pct": 1.0, "stocks_pct": 0.0, "reason": "crypto_only_mode"}
    assert client.calls == []


def test_allocate_returns_a_crypto_only_allocation(client_factory):
    client = client_factory({"crypto_pct": 0.2, "stocks_pct": 0.8})
    allocation = Allocator(CONFIG, client=client).allocate()
    assert isinstance(allocation, Allocation)
    assert allocation.crypto_pct == 1.0
    assert allocation.stocks_pct == 0.0
    assert allocation.reason == "crypto_only_mode"


def test_allocation_normalized_handles_a_degenerate_split():
    allocation = Allocation(crypto_pct=0.0, stocks_pct=0.0).normalized()
    assert allocation.crypto_pct == 1.0
    assert allocation.stocks_pct == 0.0
