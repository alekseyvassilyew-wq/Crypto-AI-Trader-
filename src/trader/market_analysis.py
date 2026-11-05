"""Broad-market breadth summary built only from Kraken's live ticker snapshot."""
import math

_STABLE_OR_FIAT = {
    "EUR", "USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK",
    "DKK", "PLN", "CZK", "HUF", "RON", "TRY", "MXN", "BRL", "SGD", "HKD",
    "USDT", "USDC", "DAI", "TUSD", "USDP", "PYUSD", "EURT", "EURC",
}
_QUOTE_PRIORITY = {"EUR": 0, "USD": 1, "USDT": 2, "USDC": 3}


def summarize_market_snapshot(tickers, analysis_rows=()):
    """Summarize unique non-fiat assets; day change is UTC-day, not rolling 24h."""
    by_asset = {}
    for ticker in tickers or ():
        if not isinstance(ticker, dict):
            continue
        base = str(ticker.get("base") or "").upper()
        quote = str(ticker.get("quote") or "").upper()
        if not base or base in _STABLE_OR_FIAT or not quote:
            continue
        try:
            change = float(ticker.get("dayChangePct"))
            turnover = float(ticker.get("turnover24h") or 0.0)
            last = float(ticker.get("lastPrice") or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (change, turnover, last)):
            continue
        if last <= 0 or turnover < 0:
            continue
        item = dict(ticker)
        item["dayChangePct"] = change
        priority = _QUOTE_PRIORITY.get(quote, 10)
        previous = by_asset.get(base)
        if previous is None or (priority, -turnover) < previous[0]:
            by_asset[base] = ((priority, -turnover), item)

    assets = [entry[1] for entry in by_asset.values()]
    changes = [item["dayChangePct"] for item in assets]
    up = sum(value > 0.05 for value in changes)
    down = sum(value < -0.05 for value in changes)
    flat = len(changes) - up - down
    mean_change = sum(changes) / len(changes) if changes else None
    ordered = sorted(changes)
    median_change = (
        ordered[len(ordered) // 2] if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
    ) if ordered else None
    breadth_pct = up / len(changes) * 100.0 if changes else None
    if not changes:
        regime = "INSUFFICIENT_DATA"
    elif breadth_pct >= 60 and mean_change > 0:
        regime = "BULLISH_BREADTH"
    elif breadth_pct <= 40 and mean_change < 0:
        regime = "BEARISH_BREADTH"
    else:
        regime = "MIXED"

    rows = [item for item in analysis_rows or () if item.get("ohlc_analyzed")]
    trend_counts = {
        trend: sum(1 for item in rows if item.get("trend") == trend)
        for trend in ("BULLISH", "BEARISH", "NEUTRAL")
    }
    regime_counts = {}
    for item in rows:
        label = str(item.get("market_regime") or "UNKNOWN")
        regime_counts[label] = regime_counts.get(label, 0) + 1

    def ranked(key, reverse):
        return sorted(assets, key=key, reverse=reverse)[:5]

    return {
        "asset_count": len(assets), "up": up, "down": down, "flat": flat,
        "breadth_pct": breadth_pct, "mean_change_pct": mean_change,
        "median_change_pct": median_change, "market_regime": regime,
        "gainers": sorted(
            (item for item in assets if item["dayChangePct"] > 0.05),
            key=lambda item: item["dayChangePct"], reverse=True,
        )[:5],
        "losers": sorted(
            (item for item in assets if item["dayChangePct"] < -0.05),
            key=lambda item: item["dayChangePct"],
        )[:5],
        "analyzed_count": len(rows), "trend_counts": trend_counts,
        "regime_counts": regime_counts,
    }



def select_sharp_decliners(tickers, *, min_drop_pct=-5.0, min_turnover_eur=10000.0,
                           max_spread_bps=25.0, limit=5):
    """Return liquid EUR-quoted assets with a material UTC-day decline."""
    candidates = []
    for ticker in tickers or ():
        if not isinstance(ticker, dict):
            continue
        if str(ticker.get("quote") or "").upper() != "EUR":
            continue
        base = str(ticker.get("base") or "").upper()
        if not base or base in _STABLE_OR_FIAT:
            continue
        try:
            change = float(ticker.get("dayChangePct"))
            turnover = float(ticker.get("turnover24h"))
            bid = float(ticker.get("bid1Price"))
            ask = float(ticker.get("ask1Price"))
            last = float(ticker.get("lastPrice"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(x) for x in (change, turnover, bid, ask, last)):
            continue
        if change > min_drop_pct or turnover < min_turnover_eur:
            continue
        if min(bid, ask, last) <= 0 or ask < bid:
            continue
        spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10000.0
        if spread_bps > max_spread_bps:
            continue
        item = dict(ticker)
        item["dayChangePct"] = change
        item["turnover24h"] = turnover
        item["spread_bps"] = spread_bps
        candidates.append(item)
    candidates.sort(key=lambda item: (item["dayChangePct"], -item["turnover24h"]))
    return candidates[:max(0, int(limit))]


def should_alert_dip_status(previous, status, now_epoch, *, cooldown_seconds=21600):
    """Alert on a new classification or repeat it only after a bounded cooldown."""
    if status not in {"POTENTIAL_BUY", "WATCH", "REJECT"}:
        return False
    try:
        now = float(now_epoch)
        cooldown = float(cooldown_seconds)
        last_alert = float((previous or {}).get("last_alert_epoch", 0.0))
    except (TypeError, ValueError, OverflowError, AttributeError):
        return True
    if not all(math.isfinite(value) for value in (now, cooldown, last_alert)) or cooldown < 0:
        return True
    if (previous or {}).get("status") != status:
        return True
    return now - last_alert >= cooldown


def evaluate_dip_candidate(ticker, frames, *, min_turnover_eur=10000.0,
                           max_spread_bps=25.0):
    """Classify a falling asset without creating or authorizing an order."""
    reasons = []
    try:
        change = float(ticker.get("dayChangePct"))
        turnover = float(ticker.get("turnover_eur", ticker.get("turnover24h", 0.0)))
        spread = float(ticker.get("spread_bps", 999999.0))
    except (TypeError, ValueError, OverflowError):
        return {"status": "REJECT", "score": 0, "reasons": ["Некорректные данные рынка"]}
    if not all(math.isfinite(x) for x in (change, turnover, spread)):
        return {"status": "REJECT", "score": 0, "reasons": ["Некорректные числовые данные"]}
    if turnover < min_turnover_eur:
        return {"status": "REJECT", "score": 0, "reasons": ["Недостаточная ликвидность"]}
    if spread > max_spread_bps:
        return {"status": "REJECT", "score": 0, "reasons": ["Слишком широкий спред"]}
    if not isinstance(frames, dict) or any(
        not isinstance(frames.get(key), dict) or frames[key].get("market_regime") == "UNKNOWN"
        for key in ("1h", "4h", "1d")
    ):
        return {"status": "WATCH", "score": 0, "reasons": ["Недостаточно качественных данных по таймфреймам"]}

    one, four, daily = frames["1h"], frames["4h"], frames["1d"]
    score = 0
    if one.get("trend") == "BULLISH":
        score += 2
        reasons.append("1h: восходящий тренд — есть признак восстановления")
    else:
        reasons.append("1h: разворот вверх пока не подтверждён")
    if four.get("trend") in {"BULLISH", "NEUTRAL"}:
        score += 1
        reasons.append("4h: давление продавцов ослабевает или нейтрально")
    else:
        score -= 2
        reasons.append("4h: сохраняется нисходящий тренд")
    if daily.get("trend") in {"BULLISH", "NEUTRAL"}:
        score += 1
        reasons.append("1d: нет подтверждённого сильного нисходящего тренда")
    else:
        score -= 2
        reasons.append("1d: дневной тренд остаётся нисходящим")
    try:
        rsi_value = float(one.get("rsi"))
        if math.isfinite(rsi_value) and 35.0 <= rsi_value <= 60.0:
            score += 1
            reasons.append(f"RSI 1h {rsi_value:.1f}: зона возможного восстановления")
        elif math.isfinite(rsi_value) and rsi_value < 30.0:
            reasons.append(f"RSI 1h {rsi_value:.1f}: перепроданность сама по себе не подтверждает покупку")
        else:
            reasons.append("RSI 1h не подтверждает комфортную точку входа")
    except (TypeError, ValueError, OverflowError):
        reasons.append("RSI 1h недоступен")
    try:
        momentum = float(one.get("momentum_24_percent"))
        if math.isfinite(momentum) and momentum > 0:
            score += 2
            reasons.append(f"Импульс 1h за 24 свечи: {momentum:+.2f}%")
        elif math.isfinite(momentum) and momentum < -5:
            score -= 2
            reasons.append(f"Падение продолжается: импульс {momentum:+.2f}%")
        else:
            reasons.append(f"Импульс 1h: {momentum:+.2f}% — отскок ещё слабый")
    except (TypeError, ValueError, OverflowError):
        reasons.append("Импульс 1h недоступен")
    try:
        volume_ratio = float(one.get("volume_ratio"))
        if math.isfinite(volume_ratio) and volume_ratio >= 1.2 and one.get("trend") == "BULLISH":
            score += 1
            reasons.append(f"Объём подтверждает движение: {volume_ratio:.2f}x")
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        atr = float(one.get("atr_percent"))
        if not math.isfinite(atr) or atr >= 8.0:
            score -= 2
            reasons.append("Очень высокая волатильность: повышенный риск")
    except (TypeError, ValueError, OverflowError):
        score -= 1
        reasons.append("ATR 1h недоступен")
    if four.get("trend") == "BEARISH" and daily.get("trend") == "BEARISH":
        return {"status": "REJECT", "score": score, "reasons": reasons + ["Нисходящий тренд одновременно на 4h и 1d"]}
    if one.get("trend") == "BULLISH" and four.get("trend") != "BEARISH" and daily.get("trend") != "BEARISH" and score >= 5:
        status = "POTENTIAL_BUY"
        reasons.append("Есть предварительное подтверждение отскока; это не торговый приказ")
    else:
        status = "WATCH"
        reasons.append("Наблюдать: условия разворота ещё не собраны")
    return {"status": status, "score": score, "reasons": reasons}
