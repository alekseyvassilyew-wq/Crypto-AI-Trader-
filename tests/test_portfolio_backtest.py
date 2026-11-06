import pytest

from src.trader.portfolio_backtest import run_portfolio_backtest


def flat_candles(count=64, price=100.0):
    return [
        [float(i), price, price + 1, price - 1, price, 1000.0]
        for i in range(count)
    ]


def buy_on_first_eligible_close(symbol, history):
    return {"signal": "BUY" if len(history) == 61 else "HOLD", "score": 10}


def test_portfolio_backtest_shares_cash_across_eur_and_usd_markets():
    result = run_portfolio_backtest(
        {"BTCEUR": flat_candles(price=100), "ETHUSD": flat_candles(price=100)},
        quote_currencies={"BTCEUR": "EUR", "ETHUSD": "USD"},
        fx_rates={"EUR": 1.0, "USD": 0.9},
        initial_cash_eur=1000,
        max_trade_eur=100,
        max_positions=2,
        fee_rate=0.001,
        slippage_bps=0,
        signal_fn=buy_on_first_eligible_close,
    )

    assert result["markets"] == ["BTCEUR", "ETHUSD"]
    assert result["trades"] == 2
    assert {trade["symbol"] for trade in result["trades_detail"]} == {"BTCEUR", "ETHUSD"}
    assert result["final_equity_eur"] < result["initial_equity_eur"]
    assert result["fees_paid_eur"] > 0
    usd_trade = next(trade for trade in result["trades_detail"] if trade["symbol"] == "ETHUSD")
    assert usd_trade["conversion_fee_eur"] > 0
    assert usd_trade["fees_eur"] == pytest.approx(
        usd_trade["conversion_fee_eur"] + usd_trade["entry_fee_eur"] + usd_trade["exit_fee_eur"]
    )
    assert result["cash_by_currency"]["EUR"] >= 0
    assert result["cash_by_currency"]["USD"] >= 0


def test_portfolio_daily_loss_limit_liquidates_open_positions_and_halts_entries():
    rows = flat_candles(count=65)
    rows[62] = [62.0, 100.0, 101.0, 99.0, 99.4, 1000.0]
    rows[63] = [63.0, 99.4, 100.0, 99.0, 99.5, 1000.0]
    rows[64] = [64.0, 99.5, 100.0, 99.0, 99.5, 1000.0]

    result = run_portfolio_backtest(
        {"BTCEUR": rows},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=50,
        max_positions=1,
        risk_per_trade_pct=0.5,
        fee_rate=0,
        slippage_bps=0,
        stop_loss_pct=2,
        take_profit_pct=50,
        trailing_stop_pct=10,
        max_daily_loss_eur=0.1,
        signal_fn=buy_on_first_eligible_close,
    )

    assert result["trades"] == 1
    assert result["trades_detail"][0]["reason"] == "DAILY_LOSS_LIMIT"
    assert result["trades_detail"][0]["exit_price"] == pytest.approx(99.4)
    assert result["open_positions"] == 0
    assert result["final_equity_eur"] < 100


def test_portfolio_backtest_no_signal_does_not_create_profit():
    result = run_portfolio_backtest(
        {"BTCEUR": flat_candles()},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=50,
        signal_fn=lambda symbol, history: {"signal": "HOLD"},
    )
    assert result["trades"] == 0
    assert result["net_pnl_eur"] == pytest.approx(0)
    assert result["final_equity_eur"] == pytest.approx(100)


def test_portfolio_backtest_rejects_misaligned_market_candles():
    second = flat_candles()
    second[-1][0] += 1
    with pytest.raises(ValueError, match="identical candle timestamps"):
        run_portfolio_backtest(
            {"BTCEUR": flat_candles(), "ETHUSD": second},
            quote_currencies={"BTCEUR": "EUR", "ETHUSD": "USD"},
            fx_rates={"EUR": 1.0, "USD": 0.9},
        )


def test_portfolio_backtest_rejects_missing_candles_even_when_all_markets_share_the_gap():
    gapped = flat_candles()
    del gapped[20]
    with pytest.raises(ValueError, match="history has a gap"):
        run_portfolio_backtest(
            {"BTCEUR": gapped, "ETHUSD": gapped},
            quote_currencies={"BTCEUR": "EUR", "ETHUSD": "USD"},
            fx_rates={"EUR": 1.0, "USD": 0.9},
        )


def test_portfolio_backtest_validates_explicit_candle_interval():
    candles = [[float(i * 60), 100.0, 101.0, 99.0, 100.0, 1000.0] for i in range(64)]
    result = run_portfolio_backtest(
        {"BTCEUR": candles},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        interval_seconds=60,
        signal_fn=lambda symbol, history: {"signal": "HOLD"},
    )
    assert result["assumptions"]["candle_interval_seconds"] == 60
    assert result["assumptions"]["candle_interval_inferred"] is False


def test_portfolio_backtest_rejects_invalid_explicit_candle_interval():
    with pytest.raises(ValueError, match="interval_seconds"):
        run_portfolio_backtest(
            {"BTCEUR": flat_candles()},
            quote_currencies={"BTCEUR": "EUR"},
            fx_rates={"EUR": 1.0},
            interval_seconds=0,
        )


def test_portfolio_backtest_rejects_unknown_quote_fx_rate():
    with pytest.raises(ValueError, match="Missing quote currency"):
        run_portfolio_backtest(
            {"ETHUSD": flat_candles()},
            quote_currencies={"ETHUSD": "USD"},
            fx_rates={"EUR": 1.0},
        )



def test_portfolio_backtest_uses_entry_and_exit_fx_rates():
    candles = [[float(i), 100.0, 100.0, 100.0, 100.0, 1000.0] for i in range(64)]
    result = run_portfolio_backtest(
        {"ETHUSD": candles},
        quote_currencies={"ETHUSD": "USD"},
        fx_rates={"EUR": 1.0, "USD": 0.9},
        fx_rates_by_timestamp={
            61.0: {"USD": 0.9},
            63.0: {"USD": 0.8},
        },
        initial_cash_eur=100,
        max_trade_eur=50,
        max_positions=1,
        fee_rate=0,
        slippage_bps=0,
        signal_fn=buy_on_first_eligible_close,
    )

    assert result["trades"] == 1
    assert result["trades_detail"][0]["net_pnl_eur"] < 0
    assert result["final_equity_eur"] < 100
    assert result["assumptions"]["dated_fx_rate_timestamps"] == 2



def test_portfolio_drawdown_includes_final_liquidation_fees():
    candles = [[float(i), 100.0, 100.0, 100.0, 100.0, 1000.0] for i in range(64)]
    result = run_portfolio_backtest(
        {"BTCEUR": candles},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=50,
        max_positions=1,
        risk_per_trade_pct=1.0,
        fee_rate=0.01,
        slippage_bps=0,
        signal_fn=buy_on_first_eligible_close,
    )

    final_loss_pct = (100 - result["final_equity_eur"]) / 100 * 100
    assert result["trades"] == 1
    assert result["final_equity_eur"] == pytest.approx(99.0)
    assert result["max_drawdown_pct"] >= final_loss_pct
    assert result["equity_curve"][-1]["equity_eur"] == pytest.approx(result["final_equity_eur"])


def test_portfolio_backtest_uses_atr_aware_risk_budget():
    def high_atr_signal(symbol, history):
        return {"signal": "BUY" if len(history) == 61 else "HOLD", "score": 5, "atr_percent": 10.0}

    result = run_portfolio_backtest(
        {"BTCEUR": flat_candles(count=64)},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=100,
        max_positions=1,
        risk_per_trade_pct=0.5,
        fee_rate=0,
        slippage_bps=0,
        stop_loss_pct=2,
        take_profit_pct=4,
        trailing_stop_pct=10,
        signal_fn=high_atr_signal,
    )
    trade = result["trades_detail"][0]
    assert trade["quantity"] * trade["entry_price"] == pytest.approx(100 * 0.005 / 0.15)


def test_portfolio_backtest_requires_integer_position_limit():
    with pytest.raises(ValueError, match="integer"):
        run_portfolio_backtest(
            {"BTCEUR": flat_candles()},
            quote_currencies={"BTCEUR": "EUR"},
            fx_rates={"EUR": 1.0},
            max_positions=1.5,
        )


def test_portfolio_backtest_checks_final_candle_stop_before_liquidation():
    rows = flat_candles(count=62)
    rows[61] = [61.0, 100.0, 101.0, 97.0, 98.0, 1000.0]
    result = run_portfolio_backtest(
        {"BTCEUR": rows},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=100,
        max_positions=1,
        risk_per_trade_pct=2.0,
        fee_rate=0,
        slippage_bps=0,
        trailing_stop_pct=10,
        signal_fn=buy_on_first_eligible_close,
    )
    assert result["trades_detail"][0]["reason"] == "STOP_LOSS"
    assert result["trades_detail"][0]["exit_price"] == pytest.approx(98.0)


def test_portfolio_backtest_drawdown_includes_intrabar_low():
    rows = flat_candles(count=64)
    rows[62] = [62.0, 100.0, 120.0, 99.0, 110.0, 1000.0]
    rows[63] = [63.0, 110.0, 111.0, 109.0, 110.0, 1000.0]
    result = run_portfolio_backtest(
        {"BTCEUR": rows},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=100,
        max_positions=1,
        risk_per_trade_pct=2.0,
        fee_rate=0,
        slippage_bps=0,
        stop_loss_pct=2,
        take_profit_pct=50,
        trailing_stop_pct=10,
        signal_fn=buy_on_first_eligible_close,
    )
    assert result["max_drawdown_pct"] >= 17.0


def test_portfolio_backtest_reports_same_window_buy_and_hold_benchmark():
    rows = flat_candles(count=64)
    rows[-1] = [63.0, 100.0, 111.0, 99.0, 110.0, 1000.0]
    result = run_portfolio_backtest(
        {"BTCEUR": rows},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=50,
        fee_rate=0,
        slippage_bps=0,
        signal_fn=lambda symbol, history: {"signal": "HOLD"},
    )
    assert result["buy_hold_return_pct"] == pytest.approx(10.0)
    assert result["buy_hold_final_equity_eur"] == pytest.approx(110.0)


def test_portfolio_final_candle_new_high_does_not_trigger_same_candle_trailing_stop():
    rows = flat_candles(62)
    rows[61] = [61.0, 100.0, 110.0, 100.0, 105.0, 1000.0]
    result = run_portfolio_backtest(
        {"BTCEUR": rows},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=20,
        max_positions=1,
        risk_per_trade_pct=2,
        fee_rate=0,
        slippage_bps=0,
        stop_loss_pct=20,
        take_profit_pct=50,
        trailing_stop_pct=5,
        signal_fn=buy_on_first_eligible_close,
    )
    trade = result["trades_detail"][0]
    assert trade["reason"] == "END_OF_DATA"
    assert trade["exit_price"] == pytest.approx(105.0)


def test_portfolio_trailing_stop_activated_on_prior_candle_triggers_next_candle():
    rows = flat_candles(63)
    rows[61] = [61.0, 100.0, 110.0, 100.0, 105.0, 1000.0]
    rows[62] = [62.0, 105.0, 106.0, 104.0, 105.0, 1000.0]
    result = run_portfolio_backtest(
        {"BTCEUR": rows},
        quote_currencies={"BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0},
        initial_cash_eur=100,
        max_trade_eur=20,
        max_positions=1,
        risk_per_trade_pct=2,
        fee_rate=0,
        slippage_bps=0,
        stop_loss_pct=20,
        take_profit_pct=50,
        trailing_stop_pct=5,
        signal_fn=buy_on_first_eligible_close,
    )
    trade = result["trades_detail"][0]
    assert trade["reason"] == "TRAILING_STOP"
    assert trade["exit_price"] == pytest.approx(104.5)

def test_portfolio_uses_executable_fx_buy_rate_and_cross_currency_funding_for_eur_quotes():
    usd_rows = flat_candles(count=64)
    # Close the first USD-quoted position at its base stop, leaving USD cash
    # alongside the remaining EUR cash for the subsequent EUR-quoted entry.
    usd_rows[61] = [61.0, 100.0, 101.0, 97.0, 98.0, 1000.0]
    eur_rows = flat_candles(count=64)

    def staged_signals(symbol, history):
        if symbol == "ETHUSD" and len(history) == 61:
            return {"signal": "BUY", "score": 10}
        if symbol == "BTCEUR" and len(history) == 62:
            return {"signal": "BUY", "score": 10}
        return {"signal": "HOLD"}

    result = run_portfolio_backtest(
        {"ETHUSD": usd_rows, "BTCEUR": eur_rows},
        quote_currencies={"ETHUSD": "USD", "BTCEUR": "EUR"},
        fx_rates={"EUR": 1.0, "USD": 0.9},
        fx_buy_rates={"EUR": 1.0, "USD": 1.1},
        initial_cash_eur=100,
        max_trade_eur=50,
        max_positions=1,
        risk_per_trade_pct=2.0,
        fee_rate=0.01,
        slippage_bps=0,
        stop_loss_pct=2,
        take_profit_pct=50,
        trailing_stop_pct=10,
        signal_fn=staged_signals,
    )

    assert result["trades"] == 2
    usd_trade = next(trade for trade in result["trades_detail"] if trade["symbol"] == "ETHUSD")
    eur_trade = next(trade for trade in result["trades_detail"] if trade["symbol"] == "BTCEUR")
    assert usd_trade["quantity"] * usd_trade["entry_price"] == pytest.approx(55.0)
    assert eur_trade["conversion_fee_eur"] > 0
    assert all(amount >= -1e-10 for amount in result["cash_by_currency"].values())
    assert result["fees_paid_eur"] == pytest.approx(
        sum(trade["fees_eur"] for trade in result["trades_detail"])
    )


def test_portfolio_backtest_uses_executable_fx_buy_rate_for_quote_allocation():
    rows = flat_candles(62)
    result = run_portfolio_backtest(
        {"ETHUSD": rows},
        quote_currencies={"ETHUSD": "USD"},
        fx_rates={"EUR": 1.0, "USD": 0.899},
        fx_buy_rates={"EUR": 1.0, "USD": 1 / 0.901},
        initial_cash_eur=100,
        max_trade_eur=10,
        max_positions=1,
        risk_per_trade_pct=2,
        fee_rate=0,
        slippage_bps=0,
        stop_loss_pct=2,
        take_profit_pct=50,
        trailing_stop_pct=10,
        signal_fn=buy_on_first_eligible_close,
    )
    trade = result["trades_detail"][0]
    assert trade["quantity"] * trade["entry_price"] == pytest.approx(10 / 0.901)
