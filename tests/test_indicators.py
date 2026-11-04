from src.trader.indicators import ema,rsi
def test_ema_has_value(): assert ema([1,2,3,4,5],3) is not None
def test_flat_rsi(): assert rsi([10]*20)==50


from src.trader.indicators import analyze, confirm_multi_timeframe


def rising_candles(count=120):
    rows = []
    for i in range(count):
        close = 100 * (1.002 ** i)
        rows.append([i * 14400.0, close * 0.999, close * 1.002, close * 0.998, close, 1000 * (1.5 if i == count - 1 else 1)])
    return rows


def test_analyze_classifies_trend_and_market_regime():
    result = analyze(rising_candles())
    assert result["trend"] == "BULLISH"
    assert result["market_regime"] in {"TREND_UP", "RANGE", "HIGH_VOLATILITY"}
    assert "ema_gap_percent" in result


def test_multi_timeframe_confirmation_requires_higher_timeframe_trend():
    base = {"signal": "BUY", "score": 5, "trend": "BULLISH", "reasons": []}
    result = confirm_multi_timeframe(base, rising_candles(), rising_candles())
    assert result["signal"] == "BUY"
    assert result["mtf_confirmed"] is True
    assert result["timeframes"]["4h"] == "BULLISH"


def test_multi_timeframe_confirmation_fails_closed_on_bearish_daily():
    base = {"signal": "BUY", "score": 5, "trend": "BULLISH", "reasons": []}
    bearish = list(reversed(rising_candles()))
    result = confirm_multi_timeframe(base, rising_candles(), bearish)
    assert result["signal"] == "HOLD"
    assert result["mtf_confirmed"] is False


def test_analyze_rejects_non_finite_and_inconsistent_candles():
    rows = rising_candles()
    rows[-1] = [rows[-1][0], rows[-1][1], float("nan"), rows[-1][3], rows[-1][4], rows[-1][5]]
    result = analyze(rows)
    assert result["signal"] == "HOLD"
    assert result["market_regime"] == "UNKNOWN"


def test_multi_timeframe_confirmation_fails_closed_on_incomplete_daily_history():
    base = {"signal": "BUY", "score": 5, "trend": "BULLISH", "reasons": []}
    result = confirm_multi_timeframe(base, rising_candles(), rising_candles(30))
    assert result["signal"] == "HOLD"
    assert result["mtf_confirmed"] is False
