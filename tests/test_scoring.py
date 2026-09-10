import pytest

from src.crypto import crypto_scoring as cs
from src.models import Token

TOKEN = Token(mint="M", symbol="X", holders=300, buys=90, sells=30, liquidity_usd=100_000)

CLEAN_AUDIT = {
    "coordinated_buys": False,
    "wash_trading": False,
    "bundled_launch": False,
    "sniper_pct": 0.0,
    "insider_pct": 0.0,
    "safety_score": 1.0,
}
STRONG_NARRATIVE = {
    "meme_score": 1.0,
    "virality": 1.0,
    "originality": 1.0,
    "community_signal": 1.0,
    "is_derivative": False,
}
OPEN_PULSE = {"go_signal": 1.0}


def test_crypto_perfect_inputs_score_one_and_buy():
    result = cs.score_token(TOKEN, CLEAN_AUDIT, STRONG_NARRATIVE, OPEN_PULSE)
    assert result["score"] == 1.0
    assert result["buy"] is True
    assert result["vetoed"] is False


def test_crypto_coordinated_buys_veto_beats_everything_else():
    audit = {**CLEAN_AUDIT, "coordinated_buys": True}
    result = cs.score_token(TOKEN, audit, STRONG_NARRATIVE, OPEN_PULSE)
    assert result["vetoed"] is True
    assert result["reason"] == "veto_coordinated_buys"
    assert result["score"] == 0.0 and result["buy"] is False


def test_crypto_wash_trading_veto():
    audit = {**CLEAN_AUDIT, "wash_trading": True}
    assert cs.score_token(TOKEN, audit, STRONG_NARRATIVE, OPEN_PULSE)["reason"] == "veto_wash_trading"


def test_crypto_paused_market_veto():
    result = cs.score_token(TOKEN, CLEAN_AUDIT, STRONG_NARRATIVE, {"go_signal": 0.29})
    assert result["reason"] == "veto_market_paused"


def test_crypto_go_signal_exactly_at_the_gate_is_not_vetoed():
    result = cs.score_token(TOKEN, CLEAN_AUDIT, STRONG_NARRATIVE, {"go_signal": 0.30})
    assert result["vetoed"] is False


def test_crypto_pessimistic_fallbacks_are_vetoed():
    from src.crypto.auditor import Auditor
    from src.crypto.crypto_pulse import CryptoPulse
    from src.crypto.narrative import Narrative

    result = cs.score_token(
        TOKEN, Auditor({}).fallback(), Narrative({}).fallback(), CryptoPulse({}).fallback()
    )
    assert result["buy"] is False and result["vetoed"] is True


def test_crypto_threshold_is_exclusive_below_inclusive_at():
    weights = {**cs.DEFAULT_WEIGHTS, "min_score_to_buy": 1.0}
    at = cs.score_token(TOKEN, CLEAN_AUDIT, STRONG_NARRATIVE, OPEN_PULSE, weights=weights)
    assert at["score"] == 1.0 and at["buy"] is True

    weights_above = {**cs.DEFAULT_WEIGHTS, "min_score_to_buy": 1.01}
    below = cs.score_token(TOKEN, CLEAN_AUDIT, STRONG_NARRATIVE, OPEN_PULSE, weights=weights_above)
    assert below["buy"] is False and below["reason"] == "below_threshold"


def test_crypto_audit_score_penalises_snipers_and_bundling():
    assert cs.audit_score(CLEAN_AUDIT) == 1.0
    assert cs.audit_score({**CLEAN_AUDIT, "sniper_pct": 0.4}) == pytest.approx(0.8)
    assert cs.audit_score({**CLEAN_AUDIT, "bundled_launch": True}) == pytest.approx(0.7)
    assert cs.audit_score({"safety_score": 0.1, "sniper_pct": 1.0}) == 0.0


def test_crypto_derivative_narrative_is_discounted():
    original = cs.narrative_score(STRONG_NARRATIVE)
    copy = cs.narrative_score({**STRONG_NARRATIVE, "is_derivative": True})
    assert copy == pytest.approx(original * 0.7)


def test_crypto_momentum_saturates_and_floors():
    assert cs.momentum_score(Token(mint="M", buys=0, sells=0, holders=0, liquidity_usd=0)) == 0.0
    hot = Token(mint="M", buys=1000, sells=1, holders=99999, liquidity_usd=1e9)
    assert cs.momentum_score(hot) == 1.0


def test_crypto_score_is_bounded_by_components():
    result = cs.score_token(
        Token(mint="M"), {"safety_score": 0.5}, {"meme_score": 0.5}, {"go_signal": 0.5}
    )
    assert 0.0 <= result["score"] <= 1.0
