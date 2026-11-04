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
    assert result["model"]["architecture"] == "7-8-1"
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

@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -0.1, 0.0, 1.0, 1.1, True])
def test_research_rejects_invalid_probability_threshold(threshold):
    with pytest.raises(ValueError, match="probability_threshold"):
        research_candidate(sample_candles(), probability_threshold=threshold)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_train": 0},
        {"min_train": -1},
        {"min_train": True},
        {"min_validation": 0},
        {"min_validation": 2.5},
        {"min_validation": False},
    ],
)
def test_research_rejects_invalid_fold_sizes(kwargs):
    with pytest.raises(ValueError, match="min_train|min_validation"):
        research_candidate(sample_candles(), **kwargs)



def test_learning_execution_uses_next_candle_open_and_two_sided_costs():
    from src.trader.learning import _net_return_after_costs

    # A 10% gap between signal close and next open must affect the entry price.
    signal_close = 100.0
    next_open = 110.0
    exit_close = 121.0
    result = _net_return_after_costs(next_open, exit_close, 0.001, 5.0)
    close_to_close = _net_return_after_costs(signal_close, exit_close, 0.001, 5.0)

    assert result < close_to_close
    assert result == pytest.approx(
        (exit_close * (1 - 0.0005) * (1 - 0.001))
        / (next_open * (1 + 0.0005) * (1 + 0.001)) - 1
    )


def test_research_candidate_declares_executable_fill_assumptions():
    result = research_candidate(sample_candles(), fee_rate=0.001, slippage_bps=2)
    assert result["assumptions"]["signal"] == "after signal candle close"
    assert result["assumptions"]["entry"] == "next candle open with adverse slippage"
    assert result["assumptions"]["exit"] == "close of horizon candle with adverse slippage"


def test_rotate_research_batch_eventually_covers_entire_market_list():
    from src.trader.learning import rotate_research_batch

    markets = [(1000 - i, f"COIN{i}EUR") for i in range(11)]
    cursor = 0
    seen = []
    for _ in range(4):
        batch, cursor = rotate_research_batch(markets, cursor, 3)
        seen.extend(symbol for _, symbol in batch)
    assert set(seen) == {symbol for _, symbol in markets}
    assert len(set(seen)) == len(markets)


def test_rotate_research_batch_handles_empty_list_and_invalid_size():
    from src.trader.learning import rotate_research_batch

    assert rotate_research_batch([], 0, 3) == ([], 0)
    with pytest.raises(ValueError, match="batch_size"):
        rotate_research_batch([("COIN", 1)], 0, 0)
