import math
import pytest

from src.trader.learning import research_candidate, predict_probability, FEATURE_NAMES


def sample_candles(count=520):
    rows = []
    for i in range(count):
        # Deterministic mixed regimes; never rely on random seeds or network data.
        close = 100 * math.exp(0.0008 * i + 0.025 * math.sin(i / 8))
        open_price = close * (1 - 0.001 * math.sin(i / 3))
        high = max(open_price, close) * 1.004
        low = min(open_price, close) * 0.996
        volume = 1000 * (1 + 0.2 * math.sin(i / 5))
        rows.append([i * 3600.0, open_price, high, low, close, volume])
    return rows


def test_research_candidate_is_walk_forward_and_shadow_only():
    result = research_candidate(sample_candles(), fee_rate=0.001, slippage_bps=2)
    assert result["status"] == "SHADOW_ONLY"
    assert len(result["walk_forward"]) == 3
    assert result["samples"] > 300
    assert result["model"]["feature_names"] == list(FEATURE_NAMES)
    assert "warning" in result
    assert "research_pass" in result
    assert 0 <= predict_probability(result["model"], [0.01] * len(FEATURE_NAMES)) <= 1


def test_research_rejects_short_or_unsorted_history():
    with pytest.raises(ValueError, match="260"):
        research_candidate(sample_candles(200))
    rows = sample_candles()
    rows[100], rows[101] = rows[101], rows[100]
    with pytest.raises(ValueError, match="chronological"):
        research_candidate(rows)


def test_research_rejects_invalid_costs():
    with pytest.raises(ValueError, match="fee or slippage"):
        research_candidate(sample_candles(), fee_rate=float("nan"))
