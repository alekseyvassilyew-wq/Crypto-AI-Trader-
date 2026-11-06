"""Walk-forward comparison of production baseline and shadow strategy ensembles.

All ensemble forecasts are generated from the candle prefix available at that
time. Adaptive weights use only forecasts whose fixed horizon has already
settled. This module is research-only and never changes execution permissions.
"""
from collections import defaultdict

from .adaptive_strategy import evaluate_adaptive_strategy_weights
from .backtest import run_backtest
from .strategy_ensemble import evaluate_strategy_ensemble
from .validation import build_walk_forward_folds

HORIZON_SECONDS = 3 * 60 * 60
STRATEGY_NAMES = (
    "trend_following", "mean_reversion", "breakout",
    "momentum", "volatility_expansion",
)


def _net_forecast_return_pct(entry_price, exit_close, fee_rate, slippage_bps):
    """Round-trip hypothetical return with the backtest's adverse cost model."""
    slip = float(slippage_bps) / 10000.0
    entry = entry_price * (1.0 + slip)
    exit_price = exit_close * (1.0 - slip)
    if entry <= 0:
        return 0.0
    net_exit = exit_price * (1.0 - float(fee_rate))
    gross_entry = entry * (1.0 + float(fee_rate))
    return (net_exit / gross_entry - 1.0) * 100.0


def _metrics_by_regime(settled):
    grouped = defaultdict(lambda: defaultdict(list))
    for item in settled:
        grouped[item["regime"]][item["strategy"]].append(item["return_pct"])
    result = {}
    for regime, strategies in grouped.items():
        result[regime] = {}
        for name, returns in strategies.items():
            equity = peak = 1.0
            max_dd = 0.0
            wins, gains, losses = 0, 0.0, 0.0
            for value in returns:
                equity *= 1.0 + value / 100.0
                peak = max(peak, equity)
                if peak > 0:
                    max_dd = max(max_dd, (peak - equity) / peak * 100.0)
                if value > 0:
                    wins += 1
                    gains += value
                elif value < 0:
                    losses += abs(value)
            result[regime][name] = {
                "resolved": len(returns),
                "net_return_pct": (equity - 1.0) * 100.0,
                "profit_factor": gains / losses if losses else ("INF" if gains else 0.0),
                "max_drawdown_pct": max_dd,
                "win_rate_pct": wins / len(returns) * 100.0 if returns else 0.0,
            }
    return result


def _infer_interval_seconds(candles):
    """Infer the candle duration from observed timestamps; tolerate data gaps."""
    deltas = sorted(
        float(candles[index][0]) - float(candles[index - 1][0])
        for index in range(1, len(candles))
        if float(candles[index][0]) > float(candles[index - 1][0])
    )
    if not deltas:
        raise ValueError("At least two increasing candle timestamps are required")
    return deltas[len(deltas) // 2]


def _walk_forward_signals(candles, *, fee_rate, slippage_bps):
    """Return equal/adaptive signal maps and auditable per-bar diagnostics."""
    interval_seconds = _infer_interval_seconds(candles)
    pending = []
    settled = []
    equal_signals = {}
    adaptive_signals = {}
    individual_signals = {name: {} for name in STRATEGY_NAMES}
    diagnostics = []

    for index, row in enumerate(candles):
        ts, close = float(row[0]), float(row[4])
        candle_close_ts = ts + interval_seconds

        # A forecast is made at this candle's close. Settle it on the first
        # candle whose CLOSE reaches its 3h target, not one full candle later.
        still_pending = []
        for forecast in pending:
            if candle_close_ts >= forecast["target_ts"]:
                value = _net_forecast_return_pct(
                    forecast["entry_price"], close, fee_rate, slippage_bps
                )
                settled.append({
                    "strategy": forecast["strategy"],
                    "regime": forecast["regime"],
                    "return_pct": value,
                    "settled_at": candle_close_ts,
                    "forecast_at": forecast["forecast_ts"],
                })
            else:
                still_pending.append(forecast)
        pending = still_pending

        # Weights are derived exclusively from forecasts settled by this close.
        current_metrics = _metrics_by_regime(settled)
        ensemble = evaluate_strategy_ensemble(candles[:index + 1])
        regime = ensemble.get("regime", "UNKNOWN")
        adaptive = evaluate_adaptive_strategy_weights(regime, current_metrics)
        strategies = ensemble.get("strategies", {})
        for name in STRATEGY_NAMES:
            opinion = strategies.get(name, {})
            individual_signals[name][ts] = {
                "signal": "BUY" if opinion.get("signal") == "BUY" else "HOLD",
                "atr_percent": ensemble.get("atr_percent", 0.0),
            }
        votes = sum(
            1 for name in STRATEGY_NAMES
            if strategies.get(name, {}).get("signal") == "BUY"
        )
        weighted_votes = sum(
            float(adaptive.get("strategies", {}).get(name, {}).get("weight", 1.0))
            for name in STRATEGY_NAMES
            if strategies.get(name, {}).get("signal") == "BUY"
        )
        equal_signals[ts] = {
            "signal": "BUY" if votes >= 2 else "HOLD",
            "atr_percent": ensemble.get("atr_percent", 0.0),
        }
        adaptive_signals[ts] = {
            "signal": "BUY" if weighted_votes >= 2.0 else "HOLD",
            "atr_percent": ensemble.get("atr_percent", 0.0),
        }
        diagnostics.append({
            "timestamp": ts,
            "candle_close_timestamp": candle_close_ts,
            "regime": regime,
            "resolved_forecasts_before_bar": len(settled),
            "weights": {
                name: adaptive.get("strategies", {}).get(name, {}).get("weight", 1.0)
                for name in STRATEGY_NAMES
            },
            "equal_buy_votes": votes,
            "adaptive_weighted_buy_votes": weighted_votes,
        })

        # Forecasts are recorded only after this bar's weights/signals are set.
        # Each BUY opinion is a distinct hypothetical forecast, never an order.
        if strategies and index + 1 < len(candles):
            # Forecasts fill at the next candle OPEN, matching run_backtest;
            # using the signal candle close here would overstate comparability.
            # Avoid overlapping 3h forecasts for the same strategy. Otherwise
            # adjacent candles would count nearly identical outcomes as
            # independent evidence and qualify adaptive weights too quickly.
            active_strategies = {item["strategy"] for item in pending}
            for name, opinion in strategies.items():
                if opinion.get("signal") == "BUY" and name not in active_strategies:
                    pending.append({
                        "strategy": name,
                        "regime": regime,
                        "forecast_ts": candle_close_ts,
                        "target_ts": candle_close_ts + HORIZON_SECONDS,
                        "entry_price": float(candles[index + 1][1]),
                    })

    return equal_signals, adaptive_signals, {
        "mode": "SHADOW_ONLY",
        "horizon_seconds": HORIZON_SECONDS,
        "candle_interval_seconds": interval_seconds,
        "settled_forecasts": len(settled),
        "unresolved_forecasts": len(pending),
        "settled_forecasts_detail": settled,
        "walk_forward": diagnostics,
        "final_metrics_by_regime": _metrics_by_regime(settled),
        "individual_signals": individual_signals,
        "execution_effect": "NONE",
        "lookahead": False,
    }



def _trade_diagnostics(result, *, interval_seconds, buy_signals=None):
    """Summarize realized trade mechanics without changing simulated execution."""
    trades = result.get("trades_detail", [])
    reasons = {}
    holding_hours = []
    gross_before_fees = 0.0
    winners, losers = [], []
    for trade in trades:
        reason = str(trade.get("reason", "UNKNOWN"))
        reasons[reason] = reasons.get(reason, 0) + 1
        gross_before_fees += (
            float(trade.get("gross_exit_notional", 0.0))
            - float(trade.get("gross_entry_notional", 0.0))
        )
        duration = max(
            0.0,
            (float(trade.get("exit_time", 0.0)) - float(trade.get("entry_time", 0.0)))
            / max(float(interval_seconds), 1.0),
        )
        holding_hours.append(duration * float(interval_seconds) / 3600.0)
        pnl = float(trade.get("net_pnl", 0.0))
        if pnl > 0:
            winners.append(pnl)
        elif pnl < 0:
            losers.append(pnl)
    return {
        "trades": len(trades),
        "buy_signals": int(buy_signals) if buy_signals is not None else None,
        "exit_reasons": dict(sorted(reasons.items())),
        "gross_pnl_after_slippage_before_fees": gross_before_fees,
        "fees_paid": float(result.get("fees_paid", 0.0)),
        "net_pnl": float(result.get("net_pnl", 0.0)),
        "net_reconciliation_difference": (
            gross_before_fees
            - float(result.get("fees_paid", 0.0))
            - float(result.get("net_pnl", 0.0))
        ),
        "average_holding_hours": (
            sum(holding_hours) / len(holding_hours) if holding_hours else 0.0
        ),
        "average_winning_trade_eur": (
            sum(winners) / len(winners) if winners else 0.0
        ),
        "average_losing_trade_eur": (
            sum(losers) / len(losers) if losers else 0.0
        ),
    }


def _evaluate_out_of_sample_folds(
    candles, *, common, fee_rate, slippage_bps, interval_seconds,
    min_train_candles=240, test_candles=120, max_folds=5,
):
    """Score strategies only inside disjoint, embargoed chronological test windows."""
    from .indicators import analyze

    embargo = max(1, (HORIZON_SECONDS + int(interval_seconds) - 1) // int(interval_seconds))
    folds = build_walk_forward_folds(
        candles, min_train_candles=min_train_candles,
        test_candles=test_candles, step_candles=test_candles,
        embargo_candles=embargo, max_folds=max_folds,
    )
    scored = []
    for number, bounds in enumerate(folds, 1):
        prefix = candles[:bounds["test_end"]]
        equal, adaptive, metadata = _walk_forward_signals(
            prefix, fee_rate=fee_rate, slippage_bps=slippage_bps
        )
        individual = metadata["individual_signals"]
        test_start_ts = float(candles[bounds["test_start"]][0])

        def gated(signal_map):
            def signal_fn(history):
                if float(history[-1][0]) < test_start_ts:
                    return {"signal": "HOLD", "atr_percent": 0.0}
                return signal_map.get(float(history[-1][0]), {"signal": "HOLD", "atr_percent": 0.0})
            return signal_fn

        def baseline_signal(history):
            if float(history[-1][0]) < test_start_ts:
                return {"signal": "HOLD", "atr_percent": 0.0}
            return analyze(history)

        results = {
            "baseline": run_backtest(prefix, **common, signal_fn=baseline_signal),
            "equal_weight_ensemble": run_backtest(prefix, **common, signal_fn=gated(equal)),
            "adaptive_ensemble": run_backtest(prefix, **common, signal_fn=gated(adaptive)),
        }
        individual_results = {
            name: run_backtest(prefix, **common, signal_fn=gated(signal_map))
            for name, signal_map in individual.items()
        }

        def compact(item):
            return {key: item[key] for key in (
                "return_pct", "net_pnl", "trades", "win_rate_pct",
                "profit_factor", "max_drawdown_pct", "fees_paid",
            )}

        scored.append({
            "fold": number,
            "train_candles": bounds["train_end"],
            "embargo_candles": embargo,
            "test_candles": bounds["test_end"] - bounds["test_start"],
            "test_start_timestamp": test_start_ts,
            "test_end_timestamp": float(candles[bounds["test_end"] - 1][0]),
            **{name: compact(item) for name, item in results.items()},
            "individual_strategies": {
                name: compact(item) for name, item in individual_results.items()
            },
        })
    return scored

def run_strategy_comparison(
    candles, *, initial_cash=1000.0, max_trade=100.0, fee_rate=0.0026,
    slippage_bps=5.0, stop_loss_pct=2.0, take_profit_pct=4.0,
    trailing_stop_pct=1.5, risk_per_trade_pct=0.5,
):
    """Compare existing baseline, equal-vote ensemble and adaptive ensemble.

    All three portfolios use identical backtest costs and risk controls.
    The baseline remains the existing analyze() strategy. Shadow ensembles
    use >=2/5 votes; adaptive votes are weighted with a threshold of 2.0.
    """
    common = {
        "initial_cash": initial_cash,
        "max_trade": max_trade,
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "trailing_stop_pct": trailing_stop_pct,
        "risk_per_trade_pct": risk_per_trade_pct,
    }
    equal, adaptive, metadata = _walk_forward_signals(
        candles, fee_rate=fee_rate, slippage_bps=slippage_bps
    )
    individual_signals = metadata.pop("individual_signals")

    def lookup_signal(signal_map, history):
        return signal_map.get(float(history[-1][0]), {"signal": "HOLD", "atr_percent": 0.0})

    baseline_result = run_backtest(candles, **common)
    equal_result = run_backtest(
        candles, **common, signal_fn=lambda history: lookup_signal(equal, history)
    )
    adaptive_result = run_backtest(
        candles, **common, signal_fn=lambda history: lookup_signal(adaptive, history)
    )
    individual_results = {
        name: run_backtest(
            candles, **common,
            signal_fn=lambda history, signals=signal_map: lookup_signal(signals, history),
        )
        for name, signal_map in individual_signals.items()
    }
    interval_seconds = metadata["candle_interval_seconds"]
    trade_diagnostics = {
        "baseline": _trade_diagnostics(baseline_result, interval_seconds=interval_seconds),
        "equal_weight_ensemble": _trade_diagnostics(equal_result, interval_seconds=interval_seconds),
        "adaptive_ensemble": _trade_diagnostics(adaptive_result, interval_seconds=interval_seconds),
        **{
            name: _trade_diagnostics(
                item,
                interval_seconds=interval_seconds,
                buy_signals=sum(1 for signal in individual_signals[name].values()
                                if signal.get("signal") == "BUY"),
            )
            for name, item in individual_results.items()
        },
    }
    out_of_sample_folds = _evaluate_out_of_sample_folds(
        candles, common=common, fee_rate=fee_rate,
        slippage_bps=slippage_bps, interval_seconds=interval_seconds,
    )
    return {
        "mode": "SHADOW_ONLY",
        "execution_effect": "NONE",
        "walk_forward_folds": out_of_sample_folds,
        "baseline": baseline_result,
        "equal_weight_ensemble": equal_result,
        "adaptive_ensemble": adaptive_result,
        "metadata": metadata,
        "individual_strategies": individual_results,
        "trade_diagnostics": trade_diagnostics,
        "comparison": {
            "same_candles": True,
            "same_costs": True,
            "same_risk_controls": True,
            "equal_vote_threshold": 2,
            "adaptive_weighted_vote_threshold": 2.0,
            "adaptive_weights_use_only_prior_settled_forecasts": True,
        },
    }
