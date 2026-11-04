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
    c = [float(x[4]) for x in data]
    h = [float(x[2]) for x in data]
    low = [float(x[3]) for x in data]
    v = [float(x[5]) for x in data]
    if len(c) < 60:
        return {"signal": "HOLD", "score": 0, "reasons": ["Not enough candles"], "market_regime": "UNKNOWN"}
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
    return {
        "signal": signal, "score": score, "price": c[-1], "rsi": rv,
        "volume_ratio": vr, "atr_percent": atrp, "trend": trend,
        "market_regime": regime, "ema_gap_percent": ema_gap,
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
        and daily.get("trend") != "BEARISH"
        and four_hour.get("market_regime") != "HIGH_VOLATILITY"
        and daily.get("market_regime") != "HIGH_VOLATILITY"
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
