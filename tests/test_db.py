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
    # A real closed fill without a reliable EUR conversion still counts as a close.
    save_trade(
        db, "PAPER", "SOLEUR", "Sell", 1.0, 10.0, 0.01, 10.0, "FILLED",
        fee_eur=None, realized_pnl_eur=None,
    )
    metrics = get_paper_metrics(db, 24)
    assert metrics["closed_trades"] == 3
    assert metrics["closed_trades_with_eur_valuation"] == 2
    assert metrics["closed_trades_without_eur_valuation"] == 1
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


def test_paper_fill_persists_wallet_and_trade_atomically(tmp_path):
    from src.trader.db import connect, get_state, save_paper_fill

    db = connect(tmp_path / "atomic-fill.sqlite3")
    wallet = {"cash": 9.9, "cash_by_currency": {"EUR": 9.9},
              "positions": {"XBTEUR": {"qty": 5.0, "avg_price": 2.0}}}
    save_paper_fill(
        db, cash_by_currency=wallet["cash_by_currency"], positions=wallet["positions"],
        symbol="XBTEUR", side="Buy", qty=5, quote=10, fee=0.1, price=2,
        fee_eur=0.1,
    )
    assert get_state(db, "paper_broker") == wallet
    row = db.execute("SELECT mode,side,qty,status FROM trades").fetchone()
    assert tuple(row) == ("PAPER", "Buy", 5.0, "FILLED")
    db.close()


def test_paper_fill_rolls_back_wallet_if_trade_insert_fails(tmp_path):
    import sqlite3
    from src.trader.db import connect, get_state, set_state, save_paper_fill

    db = connect(tmp_path / "atomic-rollback.sqlite3")
    original = {"cash": 20.0, "cash_by_currency": {"EUR": 20.0}, "positions": {}}
    set_state(db, "paper_broker", original)
    db.execute("CREATE TRIGGER reject_paper_trade BEFORE INSERT ON trades BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        save_paper_fill(
            db, cash_by_currency={"EUR": 9.9},
            positions={"XBTEUR": {"qty": 5.0, "avg_price": 2.0}},
            symbol="XBTEUR", side="Buy", qty=5, quote=10, fee=0.1, price=2,
        )
    assert get_state(db, "paper_broker") == original
    assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    db.close()


def test_strategy_shadow_predictions_settle_at_target_candle_close_and_track_net_metrics():
    from src.trader.db import (
        record_strategy_shadow_prediction,
        settle_strategy_shadow_predictions,
        get_strategy_shadow_metrics,
    )

    db = connect(":memory:")
    assert record_strategy_shadow_prediction(
        db, symbol="XBTEUR", strategy="trend_following",
        signal_ts=100, target_ts=125, entry_price=100, regime="TREND_UP",
    )
    assert not record_strategy_shadow_prediction(
        db, symbol="XBTEUR", strategy="trend_following",
        signal_ts=100, target_ts=125, entry_price=100, regime="TREND_UP",
    )
    # A second signal before the prior three-hour outcome resolves is skipped.
    assert not record_strategy_shadow_prediction(
        db, symbol="XBTEUR", strategy="trend_following",
        signal_ts=101, target_ts=126, entry_price=101, regime="TREND_UP",
    )
    assert record_strategy_shadow_prediction(
        db, symbol="XBTEUR", strategy="breakout",
        signal_ts=100, target_ts=125, entry_price=110, regime="TREND_UP",
    )

    candles = [
        [60, 100, 101, 99, 100, 10],
        [120, 101, 105, 100, 104, 12],
        [180, 102, 104, 101, 103, 11],
    ]
    assert settle_strategy_shadow_predictions(
        db, symbol="XBTEUR", candles=candles,
        fee_rate=0.01, slippage_bps=0, interval_seconds=60,
    ) == 2

    metrics = get_strategy_shadow_metrics(db, "XBTEUR")
    assert metrics["trend_following"]["resolved"] == 1
    assert metrics["trend_following"]["wins"] == 1
    assert metrics["trend_following"]["net_return_pct"] > 0
    assert metrics["breakout"]["resolved"] == 1
    assert metrics["breakout"]["wins"] == 0
    assert metrics["breakout"]["net_return_pct"] < 0
    db.close()


def test_strategy_shadow_predictions_wait_for_target_and_reject_invalid_inputs():
    from src.trader.db import (
        record_strategy_shadow_prediction,
        settle_strategy_shadow_predictions,
    )

    db = connect(":memory:")
    with pytest.raises(ValueError):
        record_strategy_shadow_prediction(
            db, symbol="XBTEUR", strategy="trend_following",
            signal_ts=10, target_ts=10, entry_price=100,
        )
    record_strategy_shadow_prediction(
        db, symbol="XBTEUR", strategy="trend_following",
        signal_ts=10, target_ts=1000, entry_price=100,
    )
    candles = [[60, 100, 101, 99, 100, 10]]
    assert settle_strategy_shadow_predictions(
        db, symbol="XBTEUR", candles=candles,
        fee_rate=0.01, slippage_bps=5, interval_seconds=60,
    ) == 0
    db.close()


def test_strategy_shadow_regime_metrics_are_separated_by_regime_and_symbol():
    from src.trader.db import record_strategy_shadow_prediction, get_strategy_shadow_regime_metrics

    db = connect(":memory:")
    record_strategy_shadow_prediction(
        db, symbol="XBTEUR", strategy="trend_following",
        signal_ts=100, target_ts=200, entry_price=100, regime="TREND_UP",
    )
    record_strategy_shadow_prediction(
        db, symbol="XBTEUR", strategy="breakout",
        signal_ts=300, target_ts=400, entry_price=100, regime="RANGE_OR_TRANSITION",
    )
    # Settle the first prediction at its target candle close.
    from src.trader.db import settle_strategy_shadow_predictions
    assert settle_strategy_shadow_predictions(
        db, symbol="XBTEUR",
        candles=[[100, 100, 103, 99, 102, 10], [200, 102, 104, 101, 103, 10]],
        fee_rate=0.0, slippage_bps=0, interval_seconds=0,
    ) == 1
    metrics = get_strategy_shadow_regime_metrics(db, symbol="XBTEUR")
    assert metrics["TREND_UP"]["trend_following"]["resolved"] == 1
    assert "breakout" not in metrics.get("TREND_UP", {})
    assert get_strategy_shadow_regime_metrics(db, symbol="OTHER") == {}
    db.close()


def test_kraken_history_import_is_idempotent_and_separate_from_paper(tmp_path):
    from src.trader.db import import_kraken_account_history, get_kraken_account_sync, save_trade

    db = connect(tmp_path / "account.sqlite3")
    trade = {
        "id": "TX-001", "ordertxid": "ORDER-1", "pair": "XBTEUR",
        "time": "1794000000.0", "type": "buy", "ordertype": "limit",
        "price": "50000", "cost": "100", "fee": "0.26", "vol": "0.002",
    }
    ledger = {
        "id": "LEDGER-001", "refid": "ORDER-1", "time": "1794000000.0",
        "type": "trade", "subtype": "", "asset": "ZEUR",
        "amount": "-100", "fee": "0.26", "balance": "900",
    }
    first = import_kraken_account_history(
        db, trades=[trade], ledgers=[ledger], balances={"ZEUR": "900"},
        fetched_at="2026-11-06T20:00:00Z", fills_complete=True, ledger_complete=True,
    )
    second = import_kraken_account_history(
        db, trades=[trade], ledgers=[ledger], balances={"ZEUR": "900"},
        fetched_at="2026-11-06T20:01:00Z", fills_complete=True, ledger_complete=True,
    )
    assert first["status"] == "COMPLETE"
    assert first["new_fills_imported"] == 1
    assert first["new_ledger_entries_imported"] == 1
    assert second["new_fills_imported"] == 0
    assert second["new_ledger_entries_imported"] == 0
    assert db.execute("SELECT COUNT(*) FROM kraken_fills").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM kraken_ledger_entries").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM trades WHERE mode='PAPER'").fetchone()[0] == 0
    sync = get_kraken_account_sync(db)
    assert sync["status"] == "COMPLETE"
    assert sync["realized_pnl_eur"] is None
    assert sync["pnl_status"] == "NOT_CALCULATED"
    db.close()


def test_kraken_history_import_marks_incomplete_history_and_does_not_claim_pnl():
    from src.trader.db import import_kraken_account_history, get_kraken_account_sync

    db = connect(":memory:")
    result = import_kraken_account_history(
        db, trades=[], ledgers=[], balances={"ZEUR": "10"},
        fetched_at="2026-11-06T20:00:00Z", fills_complete=False, ledger_complete=True,
    )
    assert result["status"] == "INCOMPLETE"
    sync = get_kraken_account_sync(db)
    assert sync["status"] == "INCOMPLETE"
    assert sync["realized_pnl_eur"] is None
    assert sync["unrealized_pnl_eur"] is None
    db.close()


def test_kraken_history_import_rejects_malformed_fill_atomically():
    from src.trader.db import import_kraken_account_history

    db = connect(":memory:")
    malformed = {
        "id": "TX-BAD", "pair": "XBTEUR", "time": "1", "type": "buy",
        "price": "NaN", "cost": "1", "fee": "0", "vol": "1",
    }
    with pytest.raises(ValueError, match="price"):
        import_kraken_account_history(
            db, trades=[malformed], ledgers=[], balances={"ZEUR": "10"},
            fetched_at="2026-11-06T20:00:00Z", fills_complete=True, ledger_complete=True,
        )
    assert db.execute("SELECT COUNT(*) FROM kraken_fills").fetchone()[0] == 0
    db.close()


def test_kraken_sync_status_unknown_before_first_import():
    from src.trader.db import get_kraken_account_sync

    db = connect(":memory:")
    assert get_kraken_account_sync(db) == {
        "status": "UNKNOWN",
        "reason": "No Kraken account history has been imported",
    }
    db.close()


def test_kraken_history_import_rejects_duplicate_ids_within_payload():
    from src.trader.db import import_kraken_account_history

    db = connect(":memory:")
    trade = {
        "id": "TX-DUP", "pair": "XBTEUR", "time": "1", "type": "buy",
        "price": "10", "cost": "10", "fee": "0", "vol": "1",
    }
    with pytest.raises(ValueError, match="Duplicate Kraken trade id"):
        import_kraken_account_history(
            db, trades=[trade, dict(trade)], ledgers=[], balances={},
            fetched_at="2026-11-06T20:00:00Z", fills_complete=True, ledger_complete=True,
        )
    assert db.execute("SELECT COUNT(*) FROM kraken_fills").fetchone()[0] == 0
    db.close()


def test_kraken_history_import_rejects_conflicting_existing_trade_payload():
    from src.trader.db import import_kraken_account_history

    db = connect(":memory:")
    trade = {
        "id": "TX-CONFLICT", "pair": "XBTEUR", "time": "1", "type": "buy",
        "price": "10", "cost": "10", "fee": "0", "vol": "1",
    }
    args = dict(
        ledgers=[], balances={}, fetched_at="2026-11-06T20:00:00Z",
        fills_complete=True, ledger_complete=True,
    )
    import_kraken_account_history(db, trades=[trade], **args)
    changed = dict(trade, price="11", cost="11")
    with pytest.raises(ValueError, match="Conflicting Kraken trade payload"):
        import_kraken_account_history(db, trades=[changed], **args)
    stored = db.execute(
        "SELECT raw_json FROM kraken_fills WHERE trade_id='TX-CONFLICT'"
    ).fetchone()[0]
    assert '"price":"10"' in stored
    db.close()


def test_kraken_history_import_rejects_duplicate_ledger_ids():
    from src.trader.db import import_kraken_account_history

    db = connect(":memory:")
    ledger = {
        "id": "LEDGER-DUP", "asset": "ZEUR", "type": "deposit",
        "time": "1", "amount": "10", "fee": "0",
    }
    with pytest.raises(ValueError, match="Duplicate Kraken ledger id"):
        import_kraken_account_history(
            db, trades=[], ledgers=[ledger, dict(ledger)], balances={},
            fetched_at="2026-11-06T20:00:00Z", fills_complete=True, ledger_complete=True,
        )
    assert db.execute("SELECT COUNT(*) FROM kraken_ledger_entries").fetchone()[0] == 0
    db.close()



def test_paper_fx_conversion_fees_are_reported_separately_and_unvalued_are_counted():
    from src.trader.db import save_paper_fx_conversion

    db = connect(":memory:")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    save_paper_fx_conversion(
        db, from_currency="EUR", to_currency="USD", spent=10.0, received=10.89,
        fee_amount=0.11, fee_currency="USD", fee_eur=0.10, legs=1,
    )
    save_paper_fx_conversion(
        db, from_currency="USD", to_currency="EUR", spent=5.0, received=4.95,
        fee_amount=0.05, fee_currency="EUR", fee_eur=None, legs=1,
    )
    metrics = get_paper_metrics(db, hours=24)
    assert metrics["fx_conversions"] == 2
    assert metrics["total_fx_conversion_fees_eur"] == pytest.approx(0.10)
    assert metrics["fx_conversions_without_eur_fee_valuation"] == 1
    db.close()

def test_paper_fill_persists_cash_eur_cost_basis_atomically():
    from src.trader.db import get_state, save_paper_fill

    db = connect(":memory:")
    save_paper_fill(
        db,
        cash_by_currency={"EUR": 10.0, "USD": 20.0},
        cash_cost_basis_eur={"EUR": 10.0, "USD": 18.0},
        positions={"ETHUSD": {
            "qty": 1.0, "avg_price": 20.0, "quote_currency": "USD",
            "peak_price": 20.0, "cost_basis_eur": 18.0, "entry_fees_eur": 0.018,
        }},
        symbol="ETHUSD", side="Buy", qty=1.0, quote=20.0,
        fee=0.02, price=20.0, fee_eur=0.018,
    )
    wallet = get_state(db, "paper_broker")
    assert wallet["cash_cost_basis_eur"] == {"EUR": 10.0, "USD": 18.0}
    assert wallet["positions"]["ETHUSD"]["cost_basis_eur"] == 18.0
    assert db.execute("SELECT COUNT(*) FROM trades WHERE mode='PAPER'").fetchone()[0] == 1
    db.close()



def test_paper_fx_conversion_persists_wallet_and_fee_ledger_atomically():
    from src.trader.db import get_state, save_paper_fx_conversion

    db = connect(":memory:")
    wallet = {
        "EUR": 5.0,
        "USD": 20.0,
    }
    basis = {
        "EUR": 5.0,
        "USD": 18.0,
    }
    positions = {
        "ETHUSD": {
            "qty": 1.0, "avg_price": 20.0, "quote_currency": "USD",
            "cost_basis_eur": 18.0, "entry_fees_eur": 0.018,
        }
    }

    save_paper_fx_conversion(
        db, from_currency="EUR", to_currency="USD",
        spent=10.0, received=9.9, fee_amount=0.1,
        fee_currency="USD", fee_eur=0.1, legs=1,
        cash_by_currency=wallet, cash_cost_basis_eur=basis, positions=positions,
    )

    saved = get_state(db, "paper_broker")
    assert saved["cash_by_currency"] == wallet
    assert saved["cash_cost_basis_eur"] == basis
    assert saved["positions"] == positions
    assert db.execute("SELECT COUNT(*) FROM paper_fx_conversions").fetchone()[0] == 1
    db.close()


def test_paper_fx_conversion_ledger_failure_rolls_back_wallet_atomically():
    import sqlite3
    from src.trader.db import get_state, save_paper_fx_conversion, set_state

    db = connect(":memory:")
    previous = {
        "cash": 20.0,
        "cash_by_currency": {"EUR": 20.0},
        "cash_cost_basis_eur": {"EUR": 20.0},
        "positions": {},
    }
    set_state(db, "paper_broker", previous)
    db.execute(
        "CREATE TRIGGER reject_paper_fx_ledger BEFORE INSERT ON paper_fx_conversions "
        "BEGIN SELECT RAISE(ABORT, 'forced FX ledger failure'); END"
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced FX ledger failure"):
        save_paper_fx_conversion(
            db, from_currency="EUR", to_currency="USD",
            spent=10.0, received=9.9, fee_amount=0.1,
            fee_currency="USD", fee_eur=0.1, legs=1,
            cash_by_currency={"EUR": 10.0, "USD": 9.9},
            cash_cost_basis_eur={"EUR": 10.0, "USD": 10.0},
            positions={},
        )

    assert get_state(db, "paper_broker") == previous
    assert db.execute("SELECT COUNT(*) FROM paper_fx_conversions").fetchone()[0] == 0
    db.close()


def test_backfill_legacy_paper_seed_fx_ledger_is_idempotent_and_does_not_touch_wallet():
    from src.trader.db import (
        audit, backfill_paper_seed_fx_ledger, get_state, save_paper_fx_conversion, set_state,
    )

    db = connect(":memory:")
    wallet = {
        "cash": 10.0,
        "cash_by_currency": {"EUR": 10.0, "USD": 5.0},
        "cash_cost_basis_eur": {"EUR": 10.0, "USD": None},
        "positions": {},
    }
    set_state(db, "paper_broker", wallet)
    audit(
        db, "PAPER_WALLET_SEED",
        "EUR->USD spent=5.00000000 received=5.55000000 fee=0.00555000",
    )
    audit(
        db, "PAPER_WALLET_SEED",
        "EUR->USDT spent=5.00000000 received=5.54500000 fee=0.00555000",
    )
    # Simulate a seed record already written by the new code before the
    # migration runs; the backfill must not add it twice.
    save_paper_fx_conversion(
        db, from_currency="EUR", to_currency="USD", spent=5.0,
        received=5.55, fee_amount=0.00555, fee_currency="USD",
        fee_eur=0.005, legs=1,
    )

    result = backfill_paper_seed_fx_ledger(db)
    assert result == {
        "already_done": False, "backfilled": 1, "already_present": 1, "unparsed": 0,
    }
    assert get_state(db, "paper_broker") == wallet
    rows = db.execute(
        "SELECT ts,from_currency,to_currency,spent,received,fee_amount,fee_currency,fee_eur,legs "
        "FROM paper_fx_conversions ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    recovered = rows[1]
    audit_ts = db.execute(
        "SELECT ts FROM audit WHERE event='PAPER_WALLET_SEED' ORDER BY id DESC LIMIT 1"
    ).fetchone()["ts"]
    assert recovered["ts"] == audit_ts
    assert recovered["to_currency"] == "USDT"
    assert recovered["fee_currency"] == "USDT"
    assert recovered["fee_amount"] == pytest.approx(0.00555)
    assert recovered["fee_eur"] == pytest.approx(0.00555 * 5.0 / (5.545 + 0.00555))
    assert recovered["legs"] == 1

    second = backfill_paper_seed_fx_ledger(db)
    assert second["already_done"] is True
    assert db.execute("SELECT COUNT(*) FROM paper_fx_conversions").fetchone()[0] == 2
    db.close()


def test_backfill_keeps_malformed_seed_audit_unresolved_for_retry():
    from src.trader.db import audit, backfill_paper_seed_fx_ledger, get_state

    db = connect(":memory:")
    audit(db, "PAPER_WALLET_SEED", "unrecognized legacy seed event")
    result = backfill_paper_seed_fx_ledger(db)

    assert result["unparsed"] == 1
    assert get_state(db, "paper_seed_fx_backfill_done", False) is False
    assert db.execute("SELECT COUNT(*) FROM paper_fx_conversions").fetchone()[0] == 0
    db.close()


def test_paper_fx_seed_audit_failure_rolls_back_wallet_and_fee_ledger():
    import sqlite3
    from src.trader.db import get_state, save_paper_fx_conversion, set_state

    db = connect(":memory:")
    previous = {
        "cash": 20.0,
        "cash_by_currency": {"EUR": 20.0},
        "cash_cost_basis_eur": {"EUR": 20.0},
        "positions": {},
    }
    set_state(db, "paper_broker", previous)
    db.execute(
        "CREATE TRIGGER reject_seed_audit BEFORE INSERT ON audit "
        "WHEN NEW.event='PAPER_WALLET_SEED' "
        "BEGIN SELECT RAISE(ABORT, 'forced seed audit failure'); END"
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced seed audit failure"):
        save_paper_fx_conversion(
            db, from_currency="EUR", to_currency="USD",
            spent=5.0, received=4.995, fee_amount=0.005,
            fee_currency="USD", fee_eur=0.005, legs=1,
            cash_by_currency={"EUR": 15.0, "USD": 4.995},
            cash_cost_basis_eur={"EUR": 15.0, "USD": 5.0},
            positions={}, audit_event="PAPER_WALLET_SEED",
            audit_details="EUR->USD spent=5.00000000 received=4.99500000 fee=0.00500000",
        )

    assert get_state(db, "paper_broker") == previous
    assert db.execute("SELECT COUNT(*) FROM paper_fx_conversions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM audit WHERE event='PAPER_WALLET_SEED'").fetchone()[0] == 0
    db.close()


def test_paper_account_journal_reconciles_baseline_fill_and_wallet():
    from src.trader.db import (
        ensure_paper_account_baseline, get_paper_account_reconciliation,
        save_paper_fill,
    )

    db = connect(":memory:")
    before = {
        "cash": 20.0, "cash_by_currency": {"EUR": 20.0},
        "cash_cost_basis_eur": {"EUR": 20.0}, "positions": {},
    }
    assert ensure_paper_account_baseline(
        db, cash_by_currency=before["cash_by_currency"],
        positions=before["positions"], cash_cost_basis_eur=before["cash_cost_basis_eur"],
    )
    after = {
        "cash": 9.99, "cash_by_currency": {"EUR": 9.99},
        "cash_cost_basis_eur": {"EUR": 9.99},
        "positions": {"XBTEUR": {
            "qty": 1.0, "avg_price": 10.0, "quote_currency": "EUR",
            "cost_basis_eur": 10.0, "entry_fees_eur": 0.01,
        }},
    }
    save_paper_fill(
        db, cash_by_currency=after["cash_by_currency"],
        cash_cost_basis_eur=after["cash_cost_basis_eur"], positions=after["positions"],
        symbol="XBTEUR", side="Buy", qty=1.0, quote=10.0, fee=0.01, price=10.0,
        fee_eur=0.01, quote_currency="EUR", wallet_before=before,
    )
    result = get_paper_account_reconciliation(db)
    assert result["status"] == "CONSISTENT_SINCE_BASELINE"
    assert result["journal_chain_valid"] is True
    assert result["current_wallet_matches_journal"] is True
    assert result["fill_events"] == 1
    assert result["cash_by_currency"] == {"EUR": 9.99}
    assert result["open_positions"][0]["symbol"] == "XBTEUR"
    assert result["full_history_reconciled"] is False
    db.close()


def test_paper_account_journal_reconciles_fx_conversion_atomically():
    from src.trader.db import (
        ensure_paper_account_baseline, get_paper_account_reconciliation,
        save_paper_fx_conversion,
    )

    db = connect(":memory:")
    before = {
        "cash": 20.0, "cash_by_currency": {"EUR": 20.0},
        "cash_cost_basis_eur": {"EUR": 20.0}, "positions": {},
    }
    ensure_paper_account_baseline(
        db, cash_by_currency=before["cash_by_currency"],
        positions=before["positions"], cash_cost_basis_eur=before["cash_cost_basis_eur"],
    )
    after = {
        "cash": 15.0,
        "cash_by_currency": {"EUR": 15.0, "USD": 5.4945},
        "cash_cost_basis_eur": {"EUR": 15.0, "USD": 5.0},
        "positions": {},
    }
    save_paper_fx_conversion(
        db, from_currency="EUR", to_currency="USD", spent=5.0,
        received=5.4945, fee_amount=0.0055, fee_currency="USD",
        fee_eur=0.005, legs=1, cash_by_currency=after["cash_by_currency"],
        cash_cost_basis_eur=after["cash_cost_basis_eur"], positions=after["positions"],
        wallet_before=before, audit_event="PAPER_FX_CONVERSION", audit_details="EUR->USD",
    )
    result = get_paper_account_reconciliation(db)
    assert result["status"] == "CONSISTENT_SINCE_BASELINE"
    assert result["fx_events"] == 1
    assert result["fx_conversion_fees_eur_since_baseline"] == pytest.approx(0.005)
    assert result["current_wallet_matches_journal"] is True
    db.close()


def test_paper_account_reconciliation_detects_broken_event_chain():
    from src.trader.db import (
        ensure_paper_account_baseline, get_paper_account_reconciliation,
        save_paper_fill,
    )

    db = connect(":memory:")
    before = {
        "cash": 20.0, "cash_by_currency": {"EUR": 20.0},
        "cash_cost_basis_eur": {"EUR": 20.0}, "positions": {},
    }
    ensure_paper_account_baseline(
        db, cash_by_currency=before["cash_by_currency"],
        positions=before["positions"], cash_cost_basis_eur=before["cash_cost_basis_eur"],
    )
    after = {
        "cash": 9.99, "cash_by_currency": {"EUR": 9.99},
        "cash_cost_basis_eur": {"EUR": 9.99},
        "positions": {"XBTEUR": {
            "qty": 1.0, "avg_price": 10.0, "quote_currency": "EUR",
            "cost_basis_eur": 10.0, "entry_fees_eur": 0.01,
        }},
    }
    save_paper_fill(
        db, cash_by_currency=after["cash_by_currency"],
        cash_cost_basis_eur=after["cash_cost_basis_eur"], positions=after["positions"],
        symbol="XBTEUR", side="Buy", qty=1.0, quote=10.0, fee=0.01, price=10.0,
        fee_eur=0.01, quote_currency="EUR", wallet_before=before,
    )
    db.execute(
        "UPDATE paper_account_events SET wallet_before_json=? WHERE event_type='FILL'",
        ('{"cash":20,"cash_by_currency":{"EUR":19},"cash_cost_basis_eur":{"EUR":19},"positions":{}}',),
    )
    db.commit()
    result = get_paper_account_reconciliation(db)
    assert result["status"] == "INCONSISTENT"
    assert result["journal_chain_valid"] is False
    assert any("does not match prior event" in reason for reason in result["blocking_reasons"])
    db.close()
