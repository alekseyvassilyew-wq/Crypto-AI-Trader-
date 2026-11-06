import pytest

from src.trader.dip_backtest import run_dip_backtest


def candles(count, interval, *, price=100.0):
    rows = []
    for index in range(count):
        ts = float(index * interval)
        rows.append([ts, price, price * 1.01, price * 0.99, price, 1000.0])
    return rows


def test_dip_backtest_is_fee_aware_and_reports_assumptions():
    hourly = candles(100, 3600)
    four_hour = candles(80, 14400)
    daily = candles(80, 86400)

    result = run_dip_backtest(
        hourly, four_hour, daily, initial_cash=20, max_trade=5,
        fee_rate=0.0026, slippage_bps=5, assumed_spread_bps=10,
    )

    assert result["candles"] == 100
    assert result["trades"] == 0
    assert result["net_pnl"] == pytest.approx(0)
    assert result["classifications"]["FILTERED"] > 0
    assert result["assumptions"]["drop_filter"].startswith("rolling 24h")
    assert result["assumptions"]["spread_bps_assumed"] == 10


def test_dip_backtest_rejects_insufficient_higher_timeframe_history():
    with pytest.raises(ValueError, match="At least 60"):
        run_dip_backtest(candles(100, 3600), candles(59, 14400), candles(80, 86400))


def test_dip_backtest_rejects_unrealistic_spread_assumption():
    with pytest.raises(ValueError, match="exceeds"):
        run_dip_backtest(
            candles(100, 3600), candles(80, 14400), candles(80, 86400),
            max_spread_bps=25, assumed_spread_bps=30,
        )


def test_dip_backtest_does_not_use_unclosed_higher_timeframe_candles():
    hourly = candles(100, 3600)
    four_hour = candles(80, 14400)
    daily = candles(80, 86400)
    # The result must remain valid even when later HTF candles are appended:
    # they cannot enter a historical decision before their close timestamp.
    baseline = run_dip_backtest(hourly, four_hour, daily)
    extended_4h = four_hour + [[four_hour[-1][0] + 14400, 100, 101, 99, 100, 1000]]
    extended_1d = daily + [[daily[-1][0] + 86400, 100, 101, 99, 100, 1000]]
    extended = run_dip_backtest(hourly, extended_4h, extended_1d)
    assert extended["trades_detail"] == baseline["trades_detail"]
