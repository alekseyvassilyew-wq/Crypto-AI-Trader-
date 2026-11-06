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


def test_backtest_rejects_missing_candles():
    rows = candles()
    del rows[20]
    with pytest.raises(ValueError, match="history has a gap"):
        run_backtest(rows)


def test_backtest_accepts_and_reports_explicit_interval():
    rows = [[float(i * 60), 100.0, 101.0, 99.0, 100.0, 1000.0] for i in range(70)]
    result = run_backtest(
        rows,
        interval_seconds=60,
        signal_fn=lambda history: {"signal": "HOLD"},
    )
    assert result["assumptions"]["candle_interval_seconds"] == 60
    assert result["assumptions"]["candle_interval_inferred"] is False


def test_backtest_rejects_invalid_explicit_interval():
    with pytest.raises(ValueError, match="interval_seconds"):
        run_backtest(candles(), interval_seconds=0)


@pytest.mark.parametrize("fee,slippage", [(-0.1, 0), (0, -1), (float("nan"), 0), (0, 1000)])
def test_backtest_rejects_invalid_cost_assumptions(fee, slippage):
    with pytest.raises(ValueError):
        run_backtest(candles(), fee_rate=fee, slippage_bps=slippage)


def test_backtest_charges_fee_on_gross_notional_and_reconciles_round_trip():
    result = run_backtest(
        candles(), initial_cash=100, max_trade=50, fee_rate=0.01,
        slippage_bps=0, trailing_stop_pct=10.0, risk_per_trade_pct=1.0, signal_fn=buy_once,
    )
    trade = result["trades_detail"][0]
    assert trade["gross_entry_notional"] == pytest.approx(50)
    assert trade["entry_fee"] == pytest.approx(0.5)
    assert trade["gross_exit_notional"] == pytest.approx(50)
    assert trade["exit_fee"] == pytest.approx(0.5)
    assert result["fees_paid"] == pytest.approx(1.0)
    assert result["fees_paid"] == pytest.approx(result["gross_turnover"] * result["fee_rate"])
    assert result["final_equity"] == pytest.approx(99.0)


def test_backtest_reports_same_window_buy_and_hold_benchmark():
    rows = candles()
    rows[-1] = [69.0, 100.0, 111.0, 99.0, 110.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=50, fee_rate=0.01,
        slippage_bps=0, signal_fn=lambda history: {"signal": "HOLD"},
    )
    assert result["trades"] == 0
    assert result["return_pct"] == pytest.approx(0)
    assert result["buy_hold_return_pct"] > 0
    assert result["buy_hold_final_equity"] > 100


def test_backtest_fee_total_reconciles_to_gross_turnover():
    result = run_backtest(
        candles(), initial_cash=20, max_trade=5, fee_rate=0.0026,
        slippage_bps=5, signal_fn=buy_once,
    )
    assert result["fees_paid"] <= result["gross_turnover"] * result["fee_rate"] + 1e-12
    assert result["fees_paid"] == pytest.approx(result["entry_fees_paid"] + result["exit_fees_paid"])


def test_final_entry_candle_stop_is_checked_before_forced_close():
    rows = candles(62)
    rows[61] = [61.0, 100.0, 101.0, 97.0, 98.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=50, fee_rate=0.01,
        slippage_bps=0, trailing_stop_pct=10.0, signal_fn=buy_once,
    )
    trade = result["trades_detail"][0]
    assert trade["reason"] == "STOP_LOSS"
    assert trade["exit_price"] == pytest.approx(98.0)


def test_single_market_drawdown_includes_intrabar_low():
    rows = candles(64)
    rows[62] = [62.0, 100.0, 120.0, 99.0, 110.0, 1000.0]
    rows[63] = [63.0, 110.0, 111.0, 109.0, 110.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=100, fee_rate=0,
        slippage_bps=0, stop_loss_pct=2, take_profit_pct=50,
        trailing_stop_pct=10, risk_per_trade_pct=2.0, signal_fn=buy_once,
    )
    assert result["max_drawdown_pct"] >= 17.0


def test_backtest_rejects_boolean_financial_parameters():
    with pytest.raises(ValueError, match="not boolean"):
        run_backtest(candles(), initial_cash=True)


def test_single_market_backtest_uses_atr_aware_risk_budget():
    def high_atr_signal(history):
        return {
            "signal": "BUY" if len(history) == 61 else "HOLD",
            "score": 5,
            "atr_percent": 10.0,
        }

    result = run_backtest(
        candles(), initial_cash=100, max_trade=100, fee_rate=0,
        slippage_bps=0, stop_loss_pct=2, take_profit_pct=50,
        trailing_stop_pct=10, risk_per_trade_pct=0.5,
        signal_fn=high_atr_signal,
    )
    assert result["trades_detail"][0]["gross_entry_notional"] == pytest.approx(100 * 0.005 / 0.15)
    assert result["assumptions"]["risk_per_trade_pct"] == pytest.approx(0.5)


def test_buy_and_hold_benchmark_starts_at_first_strategy_entry():
    rows = candles(70)
    rows[61] = [61.0, 50.0, 51.0, 49.0, 50.0, 1000.0]
    rows[66] = [66.0, 80.0, 81.0, 79.0, 80.0, 1000.0]
    rows[69] = [69.0, 88.0, 89.0, 87.0, 88.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=50, fee_rate=0,
        slippage_bps=0, stop_loss_pct=2, take_profit_pct=50,
        trailing_stop_pct=10, risk_per_trade_pct=2,
        signal_fn=lambda history: {"signal": "BUY" if len(history) == 66 else "HOLD"},
    )
    assert result["trades_detail"][0]["entry_time"] == 66.0
    assert result["buy_hold_return_pct"] == pytest.approx(10.0)


def test_final_candle_new_high_does_not_trigger_same_candle_trailing_stop():
    rows = candles(62)
    rows[61] = [61.0, 100.0, 110.0, 100.0, 105.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=20, fee_rate=0,
        slippage_bps=0, stop_loss_pct=20, take_profit_pct=50,
        trailing_stop_pct=5, risk_per_trade_pct=2, signal_fn=buy_once,
    )
    trade = result["trades_detail"][0]
    assert trade["reason"] == "END_OF_DATA"
    assert trade["exit_price"] == pytest.approx(105.0)


def test_trailing_stop_activated_on_prior_candle_triggers_next_candle():
    rows = candles(63)
    rows[61] = [61.0, 100.0, 110.0, 100.0, 105.0, 1000.0]
    rows[62] = [62.0, 105.0, 106.0, 104.0, 105.0, 1000.0]
    result = run_backtest(
        rows, initial_cash=100, max_trade=20, fee_rate=0,
        slippage_bps=0, stop_loss_pct=20, take_profit_pct=50,
        trailing_stop_pct=5, risk_per_trade_pct=2, signal_fn=buy_once,
    )
    trade = result["trades_detail"][0]
    assert trade["reason"] == "TRAILING_STOP"
    assert trade["exit_price"] == pytest.approx(104.5)
