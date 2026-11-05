import math


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = x * k + value * (1 - k)
    return value


def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]
    up, down = sum(gains) / period, sum(losses) / period
    if down == 0:
        return 100.0 if up else 50.0
    return 100 - 100 / (1 + up / down)


def analyze(rows):
    """Analyze closed OHLCV candles; the final supplied row must be closed."""
    data = list(rows)
    if len(data) < 60:
        return {"signal": "HOLD", "score": 0, "reasons": ["Not enough candles"], "market_regime": "UNKNOWN"}
    normalized = []
    previous_ts = None
    for index, row in enumerate(data):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            return {"signal": "HOLD", "score": 0, "reasons": ["Invalid candle structure"], "market_regime": "UNKNOWN"}
        try:
            ts, op, high, low_price, close, volume = (float(row[j]) for j in (0, 1, 2, 3, 4, 5))
        except (TypeError, ValueError, OverflowError):
            return {"signal": "HOLD", "score": 0, "reasons": ["Invalid candle values"], "market_regime": "UNKNOWN"}
        if not all(math.isfinite(value) for value in (ts, op, high, low_price, close, volume)):
            return {"signal": "HOLD", "score": 0, "reasons": ["Non-finite candle values"], "market_regime": "UNKNOWN"}
        if min(op, high, low_price, close) <= 0 or volume < 0:
            return {"signal": "HOLD", "score": 0, "reasons": ["Invalid OHLCV values"], "market_regime": "UNKNOWN"}
        if low_price > min(op, close) or high < max(op, close) or low_price > high:
            return {"signal": "HOLD", "score": 0, "reasons": ["Inconsistent OHLC values"], "market_regime": "UNKNOWN"}
        if previous_ts is not None and ts <= previous_ts:
            return {"signal": "HOLD", "score": 0, "reasons": ["Candles must be strictly chronological"], "market_regime": "UNKNOWN"}
        previous_ts = ts
        normalized.append((ts, op, high, low_price, close, volume))
    c = [x[4] for x in normalized]
    h = [x[2] for x in normalized]
    low = [x[3] for x in normalized]
    v = [x[5] for x in normalized]
    e20, e50, rv = ema(c, 20), ema(c, 50), rsi(c)
    score, reasons = 0, []
    trend = "BULLISH" if c[-1] > e20 > e50 else "BEARISH" if c[-1] < e20 < e50 else "NEUTRAL"
    if trend == "BULLISH":
        score += 2
        reasons.append("Bullish EMA trend")
    else:
        reasons.append("Trend not bullish")
    if rv is not None and 50 <= rv <= 68:
        score += 1
        reasons.append(f"RSI {rv:.1f} supportive")
    else:
        reasons.append(f"RSI outside entry zone: {rv:.1f}" if rv is not None else "RSI unavailable")
    avg = sum(v[-21:-1]) / 20
    vr = v[-1] / avg if avg else 0
    if vr >= 1.2:
        score += 1
        reasons.append(f"Volume {vr:.2f}x")
    else:
        reasons.append("No volume confirmation")
    atr = sum(max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1]))
              for i in range(len(c) - 14, len(c))) / 14
    atrp = atr / c[-1] * 100 if c[-1] else 999
    if atrp < 5:
        score += 1
        reasons.append(f"ATR {atrp:.2f}%")
    else:
        reasons.append("Volatility too high")
    ema_gap = (e20 / e50 - 1) * 100 if e50 else 0
    if atrp >= 5:
        regime = "HIGH_VOLATILITY"
    elif trend == "BULLISH" and ema_gap >= 0.5:
        regime = "TREND_UP"
    elif trend == "BEARISH" and ema_gap <= -0.5:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE"
    signal = "BUY" if score >= 4 and regime != "HIGH_VOLATILITY" else "HOLD"
    # Descriptive context for human-readable analysis; these fields do not
    # alter the existing entry score or PAPER execution rules.
    recent_closes = c[-20:]
    bollinger_mid = sum(recent_closes) / len(recent_closes)
    bollinger_std = math.sqrt(sum((value - bollinger_mid) ** 2 for value in recent_closes) / len(recent_closes))
    bollinger_upper = bollinger_mid + 2.0 * bollinger_std
    bollinger_lower = bollinger_mid - 2.0 * bollinger_std
    bollinger_width = ((bollinger_upper - bollinger_lower) / bollinger_mid * 100.0) if bollinger_mid else 0.0
    bollinger_percent_b = (
        (c[-1] - bollinger_lower) / (bollinger_upper - bollinger_lower)
        if bollinger_upper > bollinger_lower else 0.5
    )
    support = min(low[-21:-1])
    resistance = max(h[-21:-1])
    momentum_24 = (c[-1] / c[-25] - 1.0) * 100.0 if len(c) >= 25 else None
    return {
        "signal": signal, "score": score, "price": c[-1], "rsi": rv,
        "volume_ratio": vr, "atr_percent": atrp, "trend": trend,
        "market_regime": regime, "ema_gap_percent": ema_gap,
        "momentum_24_percent": momentum_24,
        "bollinger_mid": bollinger_mid, "bollinger_upper": bollinger_upper,
        "bollinger_lower": bollinger_lower, "bollinger_width_percent": bollinger_width,
        "bollinger_percent_b": bollinger_percent_b,
        "support_20": support, "resistance_20": resistance,
        "reasons": reasons,
    }


def confirm_multi_timeframe(base, candles_4h, candles_1d):
    """Fail-closed higher-timeframe confirmation for a lower-timeframe BUY."""
    result = dict(base)
    reasons = list(result.get("reasons", []))
    four_hour = analyze(candles_4h)
    daily = analyze(candles_1d)
    confirmed = (
        result.get("signal") == "BUY"
        and four_hour.get("trend") == "BULLISH"
        and daily.get("trend") in {"BULLISH", "NEUTRAL"}
        and four_hour.get("market_regime") not in {"HIGH_VOLATILITY", "UNKNOWN"}
        and daily.get("market_regime") not in {"HIGH_VOLATILITY", "UNKNOWN"}
    )
    result["timeframes"] = {
        "base": result.get("trend", "UNKNOWN"),
        "4h": four_hour.get("trend", "UNKNOWN"),
        "1d": daily.get("trend", "UNKNOWN"),
    }
    result["mtf_confirmed"] = confirmed
    if result.get("signal") == "BUY" and not confirmed:
        result["signal"] = "HOLD"
        reasons.append("Higher timeframes did not confirm entry")
    elif confirmed:
        reasons.append("4h and 1d trend confirmation passed")
    result["reasons"] = reasons
    return result
