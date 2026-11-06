"""Independent rule-based strategy signals for shadow research only.

These signals are deliberately not connected to PAPER order authorization.
They are emitted side-by-side so each strategy can be evaluated independently
before any future promotion into the execution path.
"""
import math


def _ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for item in values[period:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(len(values) - period, len(values))]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _valid_candles(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) < 60:
        return None
    parsed = []
    previous_ts = None
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            return None
        try:
            ts, op, high, low, close, volume = (float(row[i]) for i in range(6))
        except (TypeError, ValueError, OverflowError):
            return None
        values = (ts, op, high, low, close, volume)
        if not all(math.isfinite(value) for value in values):
            return None
        if min(op, high, low, close) <= 0 or volume < 0:
            return None
        if low > min(op, close) or high < max(op, close) or low > high:
            return None
        if previous_ts is not None and ts <= previous_ts:
            return None
        previous_ts = ts
        parsed.append((ts, op, high, low, close, volume))
    return parsed


def evaluate_strategy_ensemble(rows):
    """Return independent BUY/HOLD opinions; never returns an executable order."""
    candles = _valid_candles(rows)
    empty = {
        "version": 1,
        "mode": "SHADOW_ONLY",
        "regime": "UNKNOWN",
        "strategies": {},
        "buy_votes": 0,
        "strategy_count": 5,
        "consensus": "NO_DATA",
    }
    if candles is None:
        return empty

    closes = [row[4] for row in candles]
    highs = [row[2] for row in candles]
    lows = [row[3] for row in candles]
    volumes = [row[5] for row in candles]
    close = closes[-1]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    rsi = _rsi(closes)
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(len(closes) - 14, len(closes))
    ]
    atr_pct = (sum(true_ranges) / 14.0) / close * 100.0
    avg_volume = sum(volumes[-21:-1]) / 20.0
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 0.0
    recent = closes[-20:]
    mean = sum(recent) / len(recent)
    variance = sum((value - mean) ** 2 for value in recent) / len(recent)
    deviation = math.sqrt(variance)
    lower = mean - 2.0 * deviation
    upper = mean + 2.0 * deviation
    percent_b = (close - lower) / (upper - lower) if upper > lower else 0.5
    ema_gap_pct = (ema20 / ema50 - 1.0) * 100.0 if ema50 else 0.0

    if atr_pct >= 5.0:
        regime = "HIGH_VOLATILITY"
    elif close > ema20 > ema50 and ema_gap_pct >= 0.25:
        regime = "TREND_UP"
    elif close < ema20 < ema50 and ema_gap_pct <= -0.25:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE_OR_TRANSITION"

    strategies = {}

    def add(name, condition, reason, score):
        strategies[name] = {
            "signal": "BUY" if condition else "HOLD",
            "score": int(score if condition else 0),
            "reason": reason,
        }

    safe_volatility = atr_pct < 5.0
    add(
        "trend_following",
        safe_volatility and close > ema20 > ema50 and rsi is not None and 48.0 <= rsi <= 70.0,
        f"EMA20/50 alignment; RSI={rsi:.1f}; ATR={atr_pct:.2f}%" if rsi is not None else "RSI unavailable",
        3,
    )
    add(
        "mean_reversion",
        safe_volatility and percent_b <= 0.10 and rsi is not None and rsi <= 35.0 and close >= closes[-2],
        f"Lower Bollinger band; %B={percent_b:.2f}; RSI={rsi:.1f}; price stabilizing" if rsi is not None else "RSI unavailable",
        2,
    )
    prior_resistance = max(highs[-21:-1])
    add(
        "breakout",
        safe_volatility and close > prior_resistance and volume_ratio >= 1.5,
        f"Close above prior 20-candle high; volume={volume_ratio:.2f}x",
        3,
    )
    momentum_12 = close / closes[-13] - 1.0
    momentum_24 = close / closes[-25] - 1.0
    add(
        "momentum",
        safe_volatility and momentum_12 > 0.01 and momentum_24 > 0.02
        and rsi is not None and 50.0 <= rsi <= 75.0,
        f"12/24-candle returns={momentum_12 * 100:+.2f}%/{momentum_24 * 100:+.2f}%; RSI={rsi:.1f}" if rsi is not None else "RSI unavailable",
        2,
    )
    # Volatility expansion is a setup filter, not a standalone directional bet.
    prior_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(len(closes) - 21, len(closes) - 1)
    ]
    prior_atr = sum(prior_ranges) / len(prior_ranges) if prior_ranges else 0.0
    current_range = max(highs[-1] - lows[-1], abs(highs[-1] - closes[-2]), abs(lows[-1] - closes[-2]))
    expansion = current_range / prior_atr if prior_atr > 0 else 0.0
    add(
        "volatility_expansion",
        safe_volatility and expansion >= 1.5 and close > closes[-2] and close > ema20 and volume_ratio >= 1.2,
        f"Range expansion={expansion:.2f}x with positive close and volume={volume_ratio:.2f}x",
        2,
    )

    votes = sum(1 for item in strategies.values() if item["signal"] == "BUY")
    consensus = "STRONG_CONFLUENCE" if votes >= 4 else "PARTIAL_CONFLUENCE" if votes >= 2 else "NO_CONFLUENCE"
    return {
        "version": 1,
        "mode": "SHADOW_ONLY",
        "regime": regime,
        "atr_percent": atr_pct,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "buy_votes": votes,
        "strategy_count": len(strategies),
        "consensus": consensus,
        "strategies": strategies,
    }
