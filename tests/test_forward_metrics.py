from src.trader.db import (
    connect, save_paper_snapshot, get_paper_metrics,
    record_shadow_prediction, settle_shadow_predictions, get_shadow_metrics,
)


def test_paper_forward_metrics_persist_equity_and_drawdown(tmp_path):
    db = connect(tmp_path / "metrics.sqlite3")
    save_paper_snapshot(db, ts="2026-11-04 10:00:00", equity_eur=20, cash={"EUR": 20},
                        open_positions=0, analyzed_markets=10, buy_signals=1)
    save_paper_snapshot(db, ts="2026-11-04 10:01:00", equity_eur=19, cash={"EUR": 19},
                        open_positions=1, analyzed_markets=10, buy_signals=0)
    report = get_paper_metrics(db, 24)
    assert report["samples"] == 2
    assert report["change_eur"] == -1
    assert report["max_observed_drawdown_pct"] == 5
    assert report["buy_signals"] == 1
    db.close()


def test_shadow_predictions_are_non_overlapping_and_settle_after_horizon(tmp_path):
    db = connect(tmp_path / "shadow.sqlite3")
    assert record_shadow_prediction(
        db, symbol="XBTEUR", signal_ts=100, target_ts=103, probability=0.8,
        threshold=0.58, entry_price=100,
    )
    assert not record_shadow_prediction(
        db, symbol="XBTEUR", signal_ts=101, target_ts=104, probability=0.9,
        threshold=0.58, entry_price=100,
    )
    candles = [
        [100, 100, 101, 99, 100, 1000],
        [101, 100, 102, 99, 101, 1000],
        [102, 101, 103, 100, 102, 1000],
        [103, 102, 111, 101, 110, 1000],
    ]
    assert settle_shadow_predictions(db, symbol="XBTEUR", candles=candles,
                                     fee_rate=0.001, slippage_bps=2) == 1
    metrics = get_shadow_metrics(db, "XBTEUR")
    assert metrics["resolved_predictions"] == 1
    assert metrics["shadow_trades"] == 1
    assert metrics["net_return_pct"] > 0
    assert metrics["pending"] == 0
    db.close()
