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
