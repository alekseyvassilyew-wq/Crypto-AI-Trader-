import math
from datetime import datetime, timezone

import pytest

from src.trader.db import connect
from src.trader.research_history import (
    store_candles, load_candles, save_research_run,
    save_research_failure, list_research_runs,
)


def candles(count=300):
    rows = []
    for i in range(count):
        close = 100 * math.exp(0.0001 * i + 0.06 * math.sin(i / 9))
        op = close * (1 - 0.001 * math.sin(i / 4))
        high = max(op, close) * 1.004
        low = min(op, close) * 0.996
        rows.append([1_700_000_000 + i * 3600, op, high, low, close, 1000.0])
    return rows


def test_research_candles_upsert_and_load_chronologically():
    db = connect(":memory:")
    rows = candles()
    assert store_candles(db, "XBT/EUR", rows) == len(rows)
    assert store_candles(db, "XBTEUR", rows[-5:]) == 5
    saved = load_candles(db, "XBTEUR")
    assert len(saved) == len(rows)
    assert saved[0][0] < saved[-1][0]
    assert saved[-1] == rows[-1]
    db.close()


def test_research_candles_reject_bad_order_and_ohlc():
    db = connect(":memory:")
    rows = candles(3)
    rows[1], rows[2] = rows[2], rows[1]
    with pytest.raises(ValueError, match="chronological"):
        store_candles(db, "XBTEUR", rows)
    rows = candles(3)
    rows[1][2] = rows[1][3] - 1
    with pytest.raises(ValueError, match="Inconsistent"):
        store_candles(db, "XBTEUR", rows)
    db.close()


def test_research_run_history_persists_metrics_and_reasons():
    db = connect(":memory:")
    candidate = {
        "symbol": "XBTEUR",
        "model_version": "v1",
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "research_pass": False,
        "samples": 500,
        "out_of_sample": {
            "net_return_pct": -1.2,
            "profit_factor": 0.8,
            "max_drawdown_pct": 17.0,
            "trades": 24,
        },
        "independent_holdout": {"net_return_pct": -0.5, "trades": 6},
        "regime_metrics": {"BULL": {"observations": 20, "net_return_pct": 1.0}},
        "rejection_reasons": ["Просадка выше лимита"],
        "model": {"weights": [[1.0]]},
    }
    save_research_run(db, candidate, source="unit-test")
    rows = list_research_runs(db, limit=5, symbol="XBTEUR")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "NOT_PASS"
    assert rows[0]["oos_return_pct"] == -1.2
    assert rows[0]["holdout_return_pct"] == -0.5
    assert rows[0]["reasons"] == ["Просадка выше лимита"]
    assert rows[0]["report"]["regime_metrics"]["BULL"]["observations"] == 20
    assert "model" not in rows[0]["report"]
    db.close()


def test_research_failures_are_also_saved():
    db = connect(":memory:")
    save_research_failure(db, "ETH/EUR", "automatic", "ValueError: insufficient data")
    rows = list_research_runs(db, limit=5)
    assert rows[0]["symbol"] == "ETH/EUR"
    assert rows[0]["outcome"] == "INSUFFICIENT_DATA"
    assert "insufficient data" in rows[0]["reasons"][0]
    db.close()
