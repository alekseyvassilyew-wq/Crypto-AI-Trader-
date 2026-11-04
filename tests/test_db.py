from datetime import datetime, timedelta, timezone

from src.trader.db import connect, get_paper_metrics, save_paper_snapshot


def test_paper_metrics_use_iso_timestamp_windows():
    db = connect(":memory:")
    now = datetime.now(timezone.utc)
    save_paper_snapshot(db, ts=(now - timedelta(hours=30)).isoformat(), equity_eur=19.0, cash={"EUR": 19.0}, open_positions=0, analyzed_markets=10, buy_signals=1)
    save_paper_snapshot(db, ts=(now - timedelta(hours=1)).isoformat(), equity_eur=20.0, cash={"EUR": 20.0}, open_positions=0, analyzed_markets=20, buy_signals=2)
    metrics = get_paper_metrics(db, hours=24)
    assert metrics["samples"] == 1
    assert metrics["start_equity_eur"] == 20.0
    db.close()
