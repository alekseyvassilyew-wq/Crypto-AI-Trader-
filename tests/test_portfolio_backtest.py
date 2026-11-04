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
    assert result["cash_by_currency"]["EUR"] >= 0
    assert result["cash_by_currency"]["USD"] >= 0


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


def test_portfolio_backtest_rejects_unknown_quote_fx_rate():
    with pytest.raises(ValueError, match="Missing quote currency"):
        run_portfolio_backtest(
            {"ETHUSD": flat_candles()},
            quote_currencies={"ETHUSD": "USD"},
            fx_rates={"EUR": 1.0},
        )
