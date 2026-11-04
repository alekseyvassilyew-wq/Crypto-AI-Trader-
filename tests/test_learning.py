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
    assert len(result["independent_holdout"]) > 0
    assert set(result["regime_metrics"]) == {"BULL", "BEAR", "SIDEWAYS"}
    assert result["diagnostics"]["walk_forward_folds"] == 3
    assert isinstance(result["rejection_reasons"], list)
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


def test_settled_shadow_prediction_retention_is_bounded():
    import time
    from src.trader.db import connect, record_shadow_prediction

    db = connect(":memory:")
    old_signal = time.time() - 91 * 86400
    record_shadow_prediction(
        db, symbol="OLDEUR", model_version="v1", signal_ts=old_signal,
        target_ts=old_signal + 3600, probability=0.8, threshold=0.6, entry_price=100,
    )
    db.execute(
        "UPDATE ml_shadow_predictions SET exit_price=105, realized_return=0.05 WHERE symbol='OLDEUR'"
    )
    now = time.time()
    record_shadow_prediction(
        db, symbol="NEWEUR", model_version="v2", signal_ts=now,
        target_ts=now + 3600, probability=0.7, threshold=0.6, entry_price=100,
    )

    assert db.execute("SELECT COUNT(*) FROM ml_shadow_predictions WHERE symbol='OLDEUR'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM ml_shadow_predictions WHERE symbol='NEWEUR'").fetchone()[0] == 1
    db.close()


def test_research_reports_probability_distribution_and_threshold_sweep():
    result = research_candidate(sample_candles(), fee_rate=0.001, slippage_bps=2)
    diagnostics = result["probability_diagnostics"]["walk_forward_oos"]
    assert diagnostics["prediction_count"] > 0
    assert 0 <= diagnostics["min"] <= diagnostics["median"] <= diagnostics["max"] <= 1
    assert set(diagnostics["thresholds"]) == {"0.50", "0.52", "0.55", "0.58", "0.60"}
    assert diagnostics["interpretation"] == "DIAGNOSTIC_ONLY_NOT_FOR_THRESHOLD_SELECTION"
    for metrics in diagnostics["thresholds"].values():
        assert "trades" in metrics and "net_return_pct" in metrics
    assert "same OOS/holdout data" in result["probability_diagnostics"]["warning"]

def test_fit_balances_imbalanced_training_labels_and_marks_scores_uncalibrated():
    from src.trader.learning import _fit

    samples = [[float(i), float(i % 3)] for i in range(20)]
    labels = [1] * 2 + [0] * 18
    model = _fit(samples, labels, epochs=5)

    assert model["training_class_counts"] == {"negative": 18, "positive": 2}
    assert model["training_class_weights"]["0"] == pytest.approx(20 / 36)
    assert model["training_class_weights"]["1"] == pytest.approx(5.0)
    assert model["score_type"] == "class_weighted_probability_score_not_calibrated_probability"
