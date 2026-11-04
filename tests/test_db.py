import pytest
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


def test_paper_snapshot_retention_uses_iso_timestamp_parsing():
    db = connect(":memory:")
    now = datetime.now(timezone.utc)
    save_paper_snapshot(
        db, ts=(now - timedelta(days=91)).isoformat(), equity_eur=18.0,
        cash={"EUR": 18.0}, open_positions=0, analyzed_markets=1, buy_signals=0,
    )
    save_paper_snapshot(
        db, ts=now.isoformat(), equity_eur=20.0,
        cash={"EUR": 20.0}, open_positions=0, analyzed_markets=1, buy_signals=0,
    )
    count = db.execute("SELECT COUNT(*) FROM paper_snapshots").fetchone()[0]
    assert count == 1
    db.close()


def test_sqlite_backup_is_integrity_checked_and_restorable(tmp_path):
    import sqlite3
    from src.trader.db import backup_database, set_state

    db = connect(":memory:")
    set_state(db, "audit_test", {"value": "preserved"})
    backup_path = backup_database(db, tmp_path / "backups")

    restored = sqlite3.connect(backup_path)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        payload = restored.execute(
            "SELECT value FROM app_state WHERE key='audit_test'"
        ).fetchone()[0]
        assert '"preserved"' in payload
    finally:
        restored.close()
        db.close()


def test_sqlite_backup_rejects_invalid_retention(tmp_path):
    from src.trader.db import backup_database

    db = connect(":memory:")
    with pytest.raises(ValueError, match="retention"):
        backup_database(db, tmp_path / "backups", keep=0)
    db.close()



def test_paper_trade_accounting_metrics_include_realized_pnl_fees_and_symbol_breakdown():
    from src.trader.db import save_trade

    db = connect(":memory:")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    save_paper_snapshot(
        db, ts=now, equity_eur=21.0, cash={"EUR": 21.0},
        open_positions=0, analyzed_markets=20, buy_signals=0,
    )
    save_trade(db, "PAPER", "XBTEUR", "Buy", 0.01, 100.0, 0.26, 10000.0, "FILLED", fee_eur=0.26)
    save_trade(
        db, "PAPER", "XBTEUR", "Sell", 0.01, 103.0, 0.2678, 10300.0, "FILLED",
        fee_eur=0.2678, realized_pnl_eur=2.4722,
    )
    save_trade(
        db, "PAPER", "ETHEUR", "Sell", 0.1, 20.0, 0.052, 200.0, "FILLED",
        fee_eur=0.052, realized_pnl_eur=-1.052,
    )
    metrics = get_paper_metrics(db, 24)
    assert metrics["closed_trades"] == 2
    assert metrics["winning_trades"] == 1
    assert metrics["losing_trades"] == 1
    assert metrics["win_rate_pct"] == 50.0
    assert metrics["realized_pnl_eur"] == pytest.approx(1.4202)
    assert metrics["total_fees_eur"] == pytest.approx(0.5798)
    assert metrics["by_symbol"][0]["symbol"] == "XBTEUR"
    db.close()


def test_legacy_trades_table_gets_additive_accounting_migration(tmp_path):
    import sqlite3
    from src.trader.db import connect

    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE trades(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, "
        "mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, "
        "quote REAL NOT NULL, fee REAL NOT NULL, price REAL NOT NULL, status TEXT NOT NULL, order_id TEXT)"
    )
    legacy.commit()
    legacy.close()

    db = connect(path)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(trades)")}
    assert {"fee_eur", "realized_pnl_eur"}.issubset(columns)
    db.close()


def test_sqlite_backup_versions_are_unique_and_retention_is_enforced(tmp_path):
    from src.trader.db import backup_database, set_state

    db = connect(tmp_path / "live.sqlite3")
    backup_dir = tmp_path / "backups"
    paths = []
    for index in range(3):
        set_state(db, "backup_generation", {"value": index})
        paths.append(backup_database(db, backup_dir, keep=2))

    assert len(set(paths)) == 3
    from pathlib import Path
    assert not Path(paths[0]).exists()  # Retention prunes the oldest generation.
    assert all(Path(path).exists() for path in paths[1:])
    retained = list(backup_dir.glob("crypto_ai_trader_*.sqlite3"))
    assert len(retained) == 2
    db.close()


def test_paper_metrics_report_actual_measurement_span_not_requested_window():
    db = connect(":memory:")
    now = datetime.now(timezone.utc)
    first = now - timedelta(hours=5, minutes=30)
    save_paper_snapshot(
        db, ts=first.isoformat(), equity_eur=20.0, cash={"EUR": 20.0},
        open_positions=0, analyzed_markets=20, buy_signals=1,
    )
    save_paper_snapshot(
        db, ts=now.isoformat(), equity_eur=19.9, cash={"EUR": 19.9},
        open_positions=0, analyzed_markets=20, buy_signals=0,
    )

    metrics = get_paper_metrics(db, hours=24)

    assert metrics["hours"] == 24
    assert metrics["samples"] == 2
    assert metrics["measurement_span_seconds"] == pytest.approx(5.5 * 3600, abs=1)
    assert metrics["measurement_span_hours"] == pytest.approx(5.5, abs=1 / 3600)
    db.close()


def test_single_paper_snapshot_has_zero_measurement_span():
    db = connect(":memory:")
    save_paper_snapshot(
        db, ts=datetime.now(timezone.utc).isoformat(), equity_eur=20.0,
        cash={"EUR": 20.0}, open_positions=0, analyzed_markets=20, buy_signals=0,
    )

    metrics = get_paper_metrics(db, hours=24)

    assert metrics["samples"] == 1
    assert metrics["measurement_span_seconds"] == 0.0
    db.close()


def test_backup_restore_reopens_paper_wallet_positions_and_history(tmp_path):
    import sqlite3
    from src.trader.db import backup_database, save_trade
    from src.trader.paper import PaperBroker

    live_db = connect(tmp_path / "live.sqlite3")
    broker = PaperBroker(20.0, 0.0026, db=live_db)
    broker.buy("XBTEUR", 5.0, 100.0, quote_to_eur_rate=1.0)
    broker.convert("EUR", "USD", 2.0, 0.9)
    save_paper_snapshot(
        live_db, ts=datetime.now(timezone.utc).isoformat(),
        equity_eur=19.9, cash=broker.cash_by_currency,
        open_positions=len(broker.positions), analyzed_markets=20, buy_signals=1,
    )
    save_trade(
        live_db, "PAPER", "XBTEUR", "Buy", 0.05, 5.0, 0.013,
        100.0, "FILLED", fee_eur=0.013,
    )

    backup_path = backup_database(live_db, tmp_path / "backups")
    live_db.close()

    with sqlite3.connect(backup_path) as raw:
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    restored_db = connect(backup_path)
    try:
        restored = PaperBroker(20.0, 0.0026, db=restored_db)
        assert restored.balance("EUR") == pytest.approx(broker.balance("EUR"))
        assert restored.balance("USD") == pytest.approx(broker.balance("USD"))
        assert restored.positions["XBTEUR"]["qty"] == pytest.approx(0.05)
        assert restored.positions["XBTEUR"]["cost_basis_eur"] == pytest.approx(5.0)
        assert restored_db.execute("SELECT COUNT(*) FROM paper_snapshots").fetchone()[0] == 1
        assert restored_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    finally:
        restored_db.close()



def test_backup_health_reports_missing_backup(tmp_path):
    from src.trader.db import get_backup_health

    health = get_backup_health(tmp_path / "backups")
    assert health["healthy"] is False
    assert health["count"] == 0
    assert health["integrity"] == "missing"
    assert health["latest_path"] is None


def test_backup_health_checks_integrity_age_and_retention(tmp_path):
    from src.trader.db import backup_database, get_backup_health

    db = connect(tmp_path / "live.sqlite3")
    backup_dir = tmp_path / "backups"
    backup_database(db, backup_dir, keep=7)
    health = get_backup_health(backup_dir, max_age_hours=26, keep=7)

    assert health["healthy"] is True
    assert health["count"] == 1
    assert health["integrity"] == "ok"
    assert health["retention_ok"] is True
    assert health["age_seconds"] < 60
    db.close()


def test_backup_health_flags_corrupt_latest_backup(tmp_path):
    from src.trader.db import get_backup_health

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "crypto_ai_trader_corrupt.sqlite3").write_bytes(b"not a sqlite database")

    health = get_backup_health(backup_dir)

    assert health["healthy"] is False
    assert health["integrity"] != "ok"



def test_latest_paper_snapshot_returns_newest_timestamp_and_equity():
    from src.trader.db import get_latest_paper_snapshot

    db = connect(":memory:")
    first = datetime.now(timezone.utc) - timedelta(minutes=2)
    second = datetime.now(timezone.utc)
    save_paper_snapshot(
        db, ts=first.isoformat(), equity_eur=19.9, cash={"EUR": 10.0},
        open_positions=1, analyzed_markets=20, buy_signals=1,
    )
    save_paper_snapshot(
        db, ts=second.isoformat(), equity_eur=19.8, cash={"EUR": 9.0},
        open_positions=2, analyzed_markets=20, buy_signals=0,
    )

    snapshot = get_latest_paper_snapshot(db)

    assert snapshot["ts"] == second.isoformat()
    assert snapshot["equity_eur"] == pytest.approx(19.8)
    assert snapshot["open_positions"] == 2
    assert snapshot["cash"] == {"EUR": 9.0}
    db.close()
