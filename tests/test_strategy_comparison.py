import pytest\n\nfrom src.trader import strategy_comparison
from src.trader.strategy_comparison import (
    HORIZON_SECONDS,
    _walk_forward_signals,
    run_strategy_comparison,
)


def candles(count=150, interval=3600):
    rows = []
    for i in range(count):
        close = 100.0 * (1.001 ** i)
        rows.append([
            i * interval, close * 0.999, close * 1.004,
            close * 0.996, close, 1200.0 if i % 7 == 0 else 1000.0,
        ])
    return rows


def always_buy(rows):
    names = (
        "trend_following", "mean_reversion", "breakout",
        "momentum", "volatility_expansion",
    )
    return {
        "mode": "SHADOW_ONLY",
        "regime": "TREND_UP",
        "atr_percent": 1.0,
        "strategies": {
            name: {"signal": "BUY", "score": 1, "reason": "test fixture"}
            for name in names
        },
    }


def test_walk_forward_diagnostics_do_not_change_when_future_data_changes():
    original = candles()
    changed = [row[:] for row in original]
    cutoff = 100
    for row in changed[cutoff + 1:]:
        row[1] *= 1.03
        row[2] *= 1.03
        row[3] *= 1.03
        row[4] *= 1.03

    _, _, first = _walk_forward_signals(original, fee_rate=0.0026, slippage_bps=5)
    _, _, second = _walk_forward_signals(changed, fee_rate=0.0026, slippage_bps=5)
    assert first["walk_forward"][:cutoff + 1] == second["walk_forward"][:cutoff + 1]


def test_walk_forward_uses_only_settled_forecasts_for_adaptive_weights(monkeypatch):
    monkeypatch.setattr(strategy_comparison, "evaluate_strategy_ensemble", always_buy)
    rows = candles()
    _, _, result = _walk_forward_signals(rows, fee_rate=0.0026, slippage_bps=5)
    assert result["mode"] == "SHADOW_ONLY"
    assert result["lookahead"] is False
    assert result["execution_effect"] == "NONE"
    diagnostics = result["walk_forward"]
    assert diagnostics[0]["resolved_forecasts_before_bar"] == 0
    assert diagnostics[1]["resolved_forecasts_before_bar"] == 0
    assert diagnostics[2]["resolved_forecasts_before_bar"] == 0
    assert diagnostics[3]["resolved_forecasts_before_bar"] > 0
    assert all(
        later["resolved_forecasts_before_bar"] >= earlier["resolved_forecasts_before_bar"]
        for earlier, later in zip(diagnostics, diagnostics[1:])
    )
    assert result["settled_forecasts"] > 0
    assert result["unresolved_forecasts"] >= 0
    assert result["candle_interval_seconds"] == 3600
    assert HORIZON_SECONDS == 10800


def test_forecasts_settle_on_first_candle_close_at_or_after_horizon(monkeypatch):
    monkeypatch.setattr(strategy_comparison, "evaluate_strategy_ensemble", always_buy)
    rows = candles(count=150, interval=3600)
    _, _, result = _walk_forward_signals(rows, fee_rate=0.0026, slippage_bps=5)
    assert result["settled_forecasts_detail"]
    for forecast in result["settled_forecasts_detail"]:
        elapsed = forecast["settled_at"] - forecast["forecast_at"]
        assert elapsed >= HORIZON_SECONDS
        assert elapsed < HORIZON_SECONDS + 3600


def test_same_strategy_forecasts_do_not_overlap(monkeypatch):
    monkeypatch.setattr(strategy_comparison, "evaluate_strategy_ensemble", always_buy)
    rows = candles(count=150, interval=3600)
    _, _, result = _walk_forward_signals(rows, fee_rate=0.0026, slippage_bps=5)
    forecasts = result["settled_forecasts_detail"]
    assert forecasts
    for strategy in {item["strategy"] for item in forecasts}:
        selected = sorted(
            (item for item in forecasts if item["strategy"] == strategy),
            key=lambda item: item["forecast_at"],
        )
        assert all(
            current["forecast_at"] >= previous["settled_at"]
            for previous, current in zip(selected, selected[1:])
        )


def test_comparison_runs_all_three_modes_with_identical_cost_assumptions():
    result = run_strategy_comparison(
        candles(), initial_cash=500, max_trade=50,
        fee_rate=0.003, slippage_bps=8,
    )
    assert result["mode"] == "SHADOW_ONLY"
    assert result["execution_effect"] == "NONE"
    assert set(result) >= {
        "baseline", "equal_weight_ensemble", "adaptive_ensemble",
        "metadata", "comparison",
    }
    assert set(result["individual_strategies"]) == {
        "trend_following", "mean_reversion", "breakout", "momentum", "volatility_expansion",
    }
    for item in result["individual_strategies"].values():
        assert item["fee_rate"] == 0.003
        assert item["assumptions"]["slippage_bps_per_side"] == 8
    for name in ("baseline", "equal_weight_ensemble", "adaptive_ensemble"):
        assert result[name]["fee_rate"] == 0.003
        assert result[name]["assumptions"]["slippage_bps_per_side"] == 8
    assert result["comparison"]["same_candles"] is True
    assert result["comparison"]["same_costs"] is True
    assert result["comparison"]["same_risk_controls"] is True


def test_comparison_rejects_too_short_history_like_base_backtest():
    rows = candles(61)
    try:
        run_strategy_comparison(rows)
    except ValueError as exc:
        assert "62" in str(exc)
    else:
        raise AssertionError("expected minimum candle validation")


def test_trade_diagnostics_reconcile_exit_reasons_costs_and_holding_time():
    from src.trader.strategy_comparison import _trade_diagnostics

    result = {
        "trades": 2,
        "fees_paid": 0.12,
        "net_pnl": -0.32,
        "trades_detail": [
            {
                "reason": "STOP_LOSS",
                "entry_time": 0,
                "exit_time": 7200,
                "gross_entry_notional": 10.0,
                "gross_exit_notional": 9.7,
                "net_pnl": -0.35,
            },
            {
                "reason": "TAKE_PROFIT",
                "entry_time": 10800,
                "exit_time": 14400,
                "gross_entry_notional": 10.0,
                "gross_exit_notional": 10.15,
                "net_pnl": 0.03,
            },
        ],
    }
    summary = _trade_diagnostics(result, interval_seconds=3600, buy_signals=7)
    assert summary["trades"] == 2
    assert summary["buy_signals"] == 7
    assert summary["exit_reasons"] == {"STOP_LOSS": 1, "TAKE_PROFIT": 1}
    assert summary["gross_pnl_after_slippage_before_fees"] ==  pytest.approx(-0.15)
    assert summary["fees_paid"] == pytest.approx(0.12)
    assert summary["net_pnl"] == pytest.approx(-0.32)
    assert summary["average_holding_hours"] == pytest.approx(1.5)
    assert summary["average_winning_trade_eur"] == pytest.approx(0.03)
    assert summary["average_losing_trade_eur"] == pytest.approx(-0.35)
