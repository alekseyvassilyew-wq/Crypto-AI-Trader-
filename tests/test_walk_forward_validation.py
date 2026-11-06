import pytest

from src.trader import strategy_comparison
from src.trader.strategy_comparison import run_walk_forward_validation


def _candles(count=350):
    rows = []
    for index in range(count):
        close = 100.0 * (1.001 ** index)
        rows.append([
            index * 3600, close * 0.999, close * 1.006,
            close * 0.994, close, 1000.0,
        ])
    return rows


def test_walk_forward_validation_gates_all_orders_until_test_window(monkeypatch):
    rows = _candles()

    def always_buy(_history):
        return {"signal": "BUY", "score": 1.0, "atr_percent": 0.2}

    def fake_walk_forward(prefix, *, fee_rate, slippage_bps):
        signals = {
            float(row[0]): {"signal": "BUY", "score": 1.0, "atr_percent": 0.2}
            for row in prefix
        }
        return signals, signals, {
            "individual_signals": {name: signals for name in strategy_comparison.STRATEGY_NAMES}
        }

    monkeypatch.setattr(strategy_comparison, "analyze", always_buy)
    monkeypatch.setattr(strategy_comparison, "_walk_forward_signals", fake_walk_forward)

    result = run_walk_forward_validation(
        rows, min_train_candles=200, test_candles=100, embargo_candles=3,
        initial_cash=1000.0, max_trade=100.0,
    )

    assert result["mode"] == "SHADOW_ONLY"
    assert result["execution_effect"] == "NONE"
    assert result["independent_fold_balances"] is True
    assert result["fold_count"] == 1
    fold = result["folds"][0]
    assert fold["train_end"] == 200
    assert fold["embargo_start"] == 200
    assert fold["embargo_end"] == 203
    assert fold["test_start"] == 203
    first_allowed_fill_time = rows[fold["test_start"] + 1][0]
    for strategy_result in result["summary"].values():
        assert strategy_result["folds"] == 1
    # The first position for each always-buy strategy must be filled only
    # after the test boundary; training and embargo candles cannot trade.
    for strategy_name in strategy_comparison.STRATEGY_NAMES:
        assert result["folds"][0]["strategies"][strategy_name]["trades"] >= 1


def test_walk_forward_validation_rejects_history_without_complete_fold():
    with pytest.raises(ValueError, match="Not enough candles"):
        run_walk_forward_validation(_candles(250), min_train_candles=200, test_candles=100)
