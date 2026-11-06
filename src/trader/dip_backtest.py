"""Historical validation for the sharp-decline rebound classifier.

The simulation uses only candles closed at each decision time. Kraken OHLCV
does not contain historical bid/ask snapshots, so spread is an explicit fixed
assumption and turnover is estimated from hourly base volume and close.
"""
import math

from .backtest import run_backtest
from .indicators import analyze
from .market_analysis import evaluate_dip_candidate


def _validate_candles(rows, name, interval_seconds):
    if not isinstance(rows, (list, tuple)) or len(rows) < 60:
        raise ValueError(f"At least 60 {name} candles are required")
    result = []
    previous = None
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError(f"Invalid {name} candle at index {index}")
        try:
            ts, op, high, low, close, volume = (float(row[i]) for i in range(6))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid {name} candle values at index {index}") from exc
        if not all(math.isfinite(x) for x in (ts, op, high, low, close, volume)):
            raise ValueError(f"Non-finite {name} candle at index {index}")
        if min(op, high, low, close) <= 0 or volume < 0:
            raise ValueError(f"Invalid {name} OHLCV at index {index}")
        if low > min(op, close) or high < max(op, close) or low > high:
            raise ValueError(f"Inconsistent {name} OHLC at index {index}")
        if previous is not None and ts <= previous:
            raise ValueError(f"{name} candles must be strictly chronological")
        previous = ts
        result.append((ts, op, high, low, close, volume))
    return result


def run_dip_backtest(
    candles_1h, candles_4h, candles_1d, *, initial_cash=20.0,
    max_trade=5.0, fee_rate=0.0026, slippage_bps=5.0,
    stop_loss_pct=2.0, take_profit_pct=4.0, trailing_stop_pct=1.5,
    risk_per_trade_pct=0.5, min_drop_pct=-5.0,
    min_turnover_eur=10000.0, max_spread_bps=25.0,
    assumed_spread_bps=10.0,
):
    """Backtest POTENTIAL_BUY dip classifications with conservative PAPER fills.

    The day-drop proxy is rolling 24h (hourly close vs 24 hours earlier), not
    Kraken's UTC-day change. Historical turnover is estimated from hourly
    volume*close. Historical spread is unavailable and uses assumed_spread_bps.
    Higher-timeframe rows are included only after their candle close.
    """
    hourly = _validate_candles(candles_1h, "1h", 3600)
    four_hour = _validate_candles(candles_4h, "4h", 14400)
    daily = _validate_candles(candles_1d, "1d", 86400)
    try:
        drop_limit = float(min_drop_pct)
        turnover_limit = float(min_turnover_eur)
        spread_assumption = float(assumed_spread_bps)
        spread_limit = float(max_spread_bps)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Dip filter assumptions must be numeric") from exc
    if not all(math.isfinite(x) for x in (drop_limit, turnover_limit, spread_assumption, spread_limit)):
        raise ValueError("Dip filter assumptions must be finite")
    if turnover_limit <= 0 or spread_assumption < 0 or spread_limit <= 0:
        raise ValueError("Invalid dip liquidity/spread assumptions")
    if spread_assumption > spread_limit:
        raise ValueError("Assumed historical spread exceeds the configured maximum")

    classifications = {"POTENTIAL_BUY": 0, "WATCH": 0, "REJECT": 0, "FILTERED": 0}
    evaluated_timestamps = []

    def dip_signal(history):
        current = history[-1]
        timestamp = float(current[0])
        index = len(history) - 1
        if index < 24:
            classifications["FILTERED"] += 1
            return {"signal": "HOLD"}
        previous_close = float(history[-25][4])
        current_close = float(current[4])
        if previous_close <= 0:
            classifications["FILTERED"] += 1
            return {"signal": "HOLD"}
        rolling_change = (current_close / previous_close - 1.0) * 100.0
        turnover = sum(float(row[5]) * float(row[4]) for row in history[-24:])
        if rolling_change > drop_limit or turnover < turnover_limit:
            classifications["FILTERED"] += 1
            return {"signal": "HOLD"}

        decision_close = timestamp + 3600.0
        closed_4h = [row for row in four_hour if row[0] + 14400.0 <= decision_close]
        closed_1d = [row for row in daily if row[0] + 86400.0 <= decision_close]
        frames = {
            "1h": analyze(history),
            "4h": analyze(closed_4h),
            "1d": analyze(closed_1d),
        }
        evaluation = evaluate_dip_candidate(
            {
                "dayChangePct": rolling_change,
                "turnover_eur": turnover,
                "spread_bps": spread_assumption,
            },
            frames,
            min_turnover_eur=turnover_limit,
            max_spread_bps=spread_limit,
        )
        status = evaluation["status"]
        classifications[status] += 1
        evaluated_timestamps.append(timestamp)
        return {
            "signal": "BUY" if status == "POTENTIAL_BUY" else "HOLD",
            "atr_percent": frames["1h"].get("atr_percent") or 0.0,
        }

    result = run_backtest(
        hourly,
        initial_cash=initial_cash,
        max_trade=max_trade,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
        risk_per_trade_pct=risk_per_trade_pct,
        signal_fn=dip_signal,
    )
    result["classifications"] = classifications
    result["evaluated_signals"] = len(evaluated_timestamps)
    result["first_signal_time"] = evaluated_timestamps[0] if evaluated_timestamps else None
    result["last_signal_time"] = evaluated_timestamps[-1] if evaluated_timestamps else None
    result["assumptions"].update({
        "strategy": "sharp-decline rebound classifier; POTENTIAL_BUY only",
        "drop_filter": "rolling 24h close-to-close, not UTC-day change",
        "turnover": "estimated sum of hourly base volume multiplied by hourly close",
        "spread_bps_assumed": spread_assumption,
        "historical_spread": "not available from Kraken OHLC; fixed assumption used",
        "higher_timeframes": "only fully closed 4h and 1d candles",
    })
    return result
