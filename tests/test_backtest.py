from src.trader.backtest import run_backtest


def test_backtest_requires_enough_closed_candles():
    try:
        run_backtest([[i, 100, 101, 99, 100, 1] for i in range(60)])
    except ValueError as exc:
        assert "61 closed candles" in str(exc)
    else:
        raise AssertionError("expected insufficient-candle validation")


def test_backtest_returns_cost_and_risk_metrics():
    rows = [[i, 100, 101, 99, 100, 10] for i in range(100)]
    result = run_backtest(rows, fee_rate=0.0026, slippage_bps=5)
    assert result["candles"] == 100
    assert result["trades"] == 0
    assert result["final_equity"] == 1000
    assert result["max_drawdown_pct"] == 0
    assert "buy_hold_pct" in result
