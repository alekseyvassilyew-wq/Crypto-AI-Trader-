import pytest

from src.trader.backtest import run_backtest


def candles(count=70):
    rows = []
    for i in range(count):
        price = 100.0
        rows.append([float(i), price, price + 1, price - 1, price, 1000.0])
    return rows


def buy_once(history):
    return {"signal": "BUY" if len(history) == 61 else "HOLD"}


def test_backtest_executes_on_next_candle_open_and_accounts_for_costs():
    rows = candles()
    rows[61] = [61.0, 110.0, 111.0, 109.0, 110.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=50, fee_rate=0.01,
        slippage_bps=10, signal_fn=buy_once,
    )
    trade = result["trades_detail"][0]
    assert trade["entry_time"] == 61.0
    assert trade["entry_price"] == pytest.approx(110.11)
    assert trade["fees"] > 0
    assert result["fees_paid"] > 0
    assert result["candles"] == 70


def test_backtest_uses_conservative_stop_when_stop_and_target_touch_same_candle():
    rows = candles()
    rows[61] = [61.0, 100.0, 105.0, 95.0, 100.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=50, fee_rate=0.0,
        slippage_bps=0, stop_loss_pct=2, take_profit_pct=4,
        signal_fn=buy_once,
    )
    trade = result["trades_detail"][0]
    assert trade["reason"] == "STOP_LOSS"
    assert trade["exit_price"] == pytest.approx(98.0)
    assert result["net_pnl"] < 0


def test_backtest_reports_flat_period_without_fake_profit():
    result = run_backtest(candles(), initial_cash=100, max_trade=50,
                          signal_fn=lambda history: {"signal": "HOLD"})
    assert result["trades"] == 0
    assert result["net_pnl"] == 0
    assert result["profit_factor"] == 0


def test_backtest_rejects_unsorted_or_invalid_candles():
    rows = candles()
    rows[10], rows[11] = rows[11], rows[10]
    with pytest.raises(ValueError, match="chronological"):
        run_backtest(rows)
    rows = candles()
    rows[4][2] = 90
    with pytest.raises(ValueError, match="Inconsistent OHLC"):
        run_backtest(rows)


@pytest.mark.parametrize("fee,slippage", [(-0.1, 0), (0, -1), (float("nan"), 0), (0, 1000)])
def test_backtest_rejects_invalid_cost_assumptions(fee, slippage):
    with pytest.raises(ValueError):
        run_backtest(candles(), fee_rate=fee, slippage_bps=slippage)
