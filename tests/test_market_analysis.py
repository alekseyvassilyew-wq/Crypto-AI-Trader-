import pytest

from src.trader.market_analysis import summarize_market_snapshot


def test_market_breadth_deduplicates_assets_and_prefers_eur_pair():
    tickers = [
        {"symbol": "XBTEUR", "base": "XBT", "quote": "EUR", "lastPrice": 110, "turnover24h": 1000, "dayChangePct": 10},
        {"symbol": "XBTUSD", "base": "XBT", "quote": "USD", "lastPrice": 111, "turnover24h": 9000, "dayChangePct": 11},
        {"symbol": "ETHEUR", "base": "ETH", "quote": "EUR", "lastPrice": 90, "turnover24h": 500, "dayChangePct": -2},
        {"symbol": "USDTEUR", "base": "USDT", "quote": "EUR", "lastPrice": 1, "turnover24h": 50000, "dayChangePct": 0.1},
    ]
    rows = [
        {"ohlc_analyzed": True, "trend": "BULLISH", "market_regime": "TREND_UP"},
        {"ohlc_analyzed": True, "trend": "BEARISH", "market_regime": "TREND_DOWN"},
        {"ohlc_analyzed": False, "trend": "UNKNOWN", "market_regime": "UNKNOWN"},
    ]

    result = summarize_market_snapshot(tickers, rows)

    assert result["asset_count"] == 2
    assert result["up"] == 1
    assert result["down"] == 1
    assert result["flat"] == 0
    assert result["market_regime"] == "MIXED"
    assert result["analyzed_count"] == 2
    assert result["trend_counts"] == {"BULLISH": 1, "BEARISH": 1, "NEUTRAL": 0}
    assert result["gainers"][0]["symbol"] == "XBTEUR"
    assert result["losers"][0]["symbol"] == "ETHEUR"


def test_market_breadth_handles_missing_or_invalid_day_change():
    result = summarize_market_snapshot([
        {"base": "XBT", "quote": "EUR", "lastPrice": 100, "turnover24h": 10},
        {"base": "ETH", "quote": "EUR", "lastPrice": 0, "turnover24h": 10, "dayChangePct": 2},
        {"base": "ADA", "quote": "EUR", "lastPrice": 1, "turnover24h": 10, "dayChangePct": float("nan")},
    ])
    assert result["asset_count"] == 0
    assert result["market_regime"] == "INSUFFICIENT_DATA"
    assert result["mean_change_pct"] is None


def test_market_leaders_do_not_label_negative_assets_as_gainers():
    result = summarize_market_snapshot([
        {"symbol": "XBTEUR", "base": "XBT", "quote": "EUR", "lastPrice": 90, "turnover24h": 100, "dayChangePct": -1.0},
        {"symbol": "ETHEUR", "base": "ETH", "quote": "EUR", "lastPrice": 80, "turnover24h": 100, "dayChangePct": -2.0},
    ])
    assert result["gainers"] == []
    assert [item["symbol"] for item in result["losers"]] == ["ETHEUR", "XBTEUR"]
