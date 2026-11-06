import math

from src.trader.strategy_ensemble import evaluate_strategy_ensemble


def candles(count=120, growth=1.002, volume_spike=True):
    rows = []
    for i in range(count):
        close = 100.0 * (growth ** i)
        volume = 1500.0 if volume_spike and i == count - 1 else 1000.0
        rows.append([
            i * 3600.0, close * 0.999, close * 1.003,
            close * 0.997, close, volume,
        ])
    return rows


def test_ensemble_emits_independent_shadow_signals_without_order_action():
    result = evaluate_strategy_ensemble(candles())
    assert result["mode"] == "SHADOW_ONLY"
    assert result["strategy_count"] == 5
    assert set(result["strategies"]) == {
        "trend_following", "mean_reversion", "breakout",
        "momentum", "volatility_expansion",
    }
    assert all(item["signal"] in {"BUY", "HOLD"} for item in result["strategies"].values())
    assert "order" not in result


def test_ensemble_fails_closed_on_bad_or_short_candle_history():
    assert evaluate_strategy_ensemble([])["consensus"] == "NO_DATA"
    rows = candles()
    rows[-1][2] = float("nan")
    result = evaluate_strategy_ensemble(rows)
    assert result["regime"] == "UNKNOWN"
    assert result["buy_votes"] == 0


def test_ensemble_rejects_non_chronological_candles():
    rows = candles()
    rows[-1][0] = rows[-2][0]
    assert evaluate_strategy_ensemble(rows)["consensus"] == "NO_DATA"


def test_high_volatility_suppresses_directional_strategy_votes():
    rows = candles(growth=1.002)
    for row in rows[-10:]:
        row[2] = row[4] * 1.08
        row[3] = row[4] * 0.92
    result = evaluate_strategy_ensemble(rows)
    assert result["regime"] == "HIGH_VOLATILITY"
    assert result["buy_votes"] == 0
