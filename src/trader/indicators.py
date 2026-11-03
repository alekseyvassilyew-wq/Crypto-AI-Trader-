def ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1)
    value = sum(values[:period])/period
    for x in values[period:]: value = x*k + value*(1-k)
    return value

def rsi(values, period=14):
    if len(values) <= period: return None
    changes = [values[i]-values[i-1] for i in range(1,len(values))]
    gains = [max(x,0) for x in changes[-period:]]
    losses = [max(-x,0) for x in changes[-period:]]
    up, down = sum(gains)/period, sum(losses)/period
    if down == 0: return 100.0 if up else 50.0
    return 100 - 100/(1+up/down)

def analyze(rows):
    # Normalized Kraken OHLC rows: oldest first.
    d = list(rows)
    c = [float(x[4]) for x in d]
    h = [float(x[2]) for x in d]
    l = [float(x[3]) for x in d]
    v = [float(x[5]) for x in d]
    if len(c) < 60: return {"signal":"HOLD","score":0,"reasons":["Not enough candles"]}
    e20, e50, rv = ema(c,20), ema(c,50), rsi(c)
    score, reasons = 0, []
    if c[-1] > e20 > e50: score += 2; reasons.append("Bullish EMA trend")
    else: reasons.append("Trend not bullish")
    if rv is not None and 50 <= rv <= 68: score += 1; reasons.append(f"RSI {rv:.1f} supportive")
    else: reasons.append(f"RSI outside entry zone: {rv:.1f}" if rv is not None else "RSI unavailable")
    avg = sum(v[-21:-1])/20
    vr = v[-1]/avg if avg else 0
    if vr >= 1.2: score += 1; reasons.append(f"Volume {vr:.2f}x")
    else: reasons.append("No volume confirmation")
    atr = sum(max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(len(c)-14,len(c)))/14
    atrp = atr/c[-1]*100 if c[-1] else 999
    if atrp < 5: score += 1; reasons.append(f"ATR {atrp:.2f}%")
    else: reasons.append("Volatility too high")
    return {"signal":"BUY" if score >= 4 else "HOLD","score":score,"price":c[-1],
            "rsi":rv,"volume_ratio":vr,"atr_percent":atrp,"reasons":reasons}
