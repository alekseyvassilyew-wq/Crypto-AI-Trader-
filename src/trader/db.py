import json
import math
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kraken_fills(
    trade_id TEXT PRIMARY KEY,
    order_id TEXT,
    pair TEXT NOT NULL,
    time REAL NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT,
    price REAL NOT NULL,
    cost REAL NOT NULL,
    fee REAL NOT NULL,
    volume REAL NOT NULL,
    raw_json TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kraken_fills_time ON kraken_fills(time);
CREATE INDEX IF NOT EXISTS idx_kraken_fills_pair_time ON kraken_fills(pair,time);
CREATE TABLE IF NOT EXISTS kraken_ledger_entries(
    ledger_id TEXT PRIMARY KEY,
    refid TEXT,
    time REAL NOT NULL,
    entry_type TEXT NOT NULL,
    subtype TEXT,
    asset TEXT NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL,
    balance REAL,
    raw_json TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kraken_ledger_time ON kraken_ledger_entries(time);
CREATE TABLE IF NOT EXISTS kraken_account_sync(
    id INTEGER PRIMARY KEY CHECK(id=1),
    fetched_at TEXT NOT NULL,
    fills_complete INTEGER NOT NULL,
    ledger_complete INTEGER NOT NULL,
    exchange_fill_count INTEGER NOT NULL,
    imported_fill_count INTEGER NOT NULL,
    exchange_ledger_count INTEGER NOT NULL,
    imported_ledger_count INTEGER NOT NULL,
    balance_json TEXT NOT NULL,
    status TEXT NOT NULL
);
\nCREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, event TEXT NOT NULL, details TEXT);
CREATE TABLE IF NOT EXISTS paper_fx_conversions(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    from_currency TEXT NOT NULL,
    to_currency TEXT NOT NULL,
    spent REAL NOT NULL,
    received REAL NOT NULL,
    fee_amount REAL,
    fee_currency TEXT,
    fee_eur REAL,
    legs INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_fx_conversions_ts ON paper_fx_conversions(ts);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, quote REAL NOT NULL, fee REAL NOT NULL, price REAL NOT NULL, status TEXT NOT NULL, order_id TEXT, fee_eur REAL, realized_pnl_eur REAL);
CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS telegram_report_pages(report_key TEXT PRIMARY KEY, pages_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_telegram_report_pages_updated ON telegram_report_pages(updated_at);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE TABLE IF NOT EXISTS paper_snapshots(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    equity_eur REAL NOT NULL,
    cash_json TEXT NOT NULL,
    open_positions INTEGER NOT NULL,
    analyzed_markets INTEGER NOT NULL,
    buy_signals INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_snapshots_ts ON paper_snapshots(ts);
CREATE TABLE IF NOT EXISTS ml_shadow_predictions(
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    model_version TEXT NOT NULL DEFAULT '',
    signal_ts REAL NOT NULL,
    target_ts REAL NOT NULL,
    probability REAL NOT NULL,
    threshold REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    realized_return REAL,
    UNIQUE(symbol, signal_ts)
);
CREATE INDEX IF NOT EXISTS idx_ml_shadow_pending ON ml_shadow_predictions(symbol, target_ts, realized_return);
CREATE TABLE IF NOT EXISTS strategy_shadow_predictions(
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    signal_ts REAL NOT NULL,
    target_ts REAL NOT NULL,
    entry_price REAL NOT NULL,
    regime TEXT NOT NULL DEFAULT '',
    exit_price REAL,
    realized_return REAL,
    UNIQUE(symbol, strategy, signal_ts)
);
CREATE INDEX IF NOT EXISTS idx_strategy_shadow_pending
ON strategy_shadow_predictions(symbol, target_ts, realized_return);
"""

def connect(path):
    raw_path = str(path)
    if raw_path == ":memory:":
        db = sqlite3.connect(":memory:", timeout=30, check_same_thread=False)
    else:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    trade_columns = {row["name"] for row in db.execute("PRAGMA table_info(trades)").fetchall()}
    if "fee_eur" not in trade_columns:
        db.execute("ALTER TABLE trades ADD COLUMN fee_eur REAL")
    if "realized_pnl_eur" not in trade_columns:
        db.execute("ALTER TABLE trades ADD COLUMN realized_pnl_eur REAL")
    # Additive migration for databases created before model-version tracking.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(ml_shadow_predictions)").fetchall()}
    if "model_version" not in columns:
        db.execute("ALTER TABLE ml_shadow_predictions ADD COLUMN model_version TEXT NOT NULL DEFAULT ''")
    return db


def backup_database(db, backup_dir, keep=7):
    """Create and verify an atomic SQLite backup, retaining the newest backups."""
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("Backup retention must be a positive integer")
    directory = Path(backup_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"crypto_ai_trader_{timestamp}.sqlite3"
    temporary = destination.with_suffix(destination.suffix + ".tmp")

    source_check = db.execute("PRAGMA quick_check").fetchone()
    if not source_check or source_check[0] != "ok":
        raise RuntimeError("Source SQLite database failed quick_check")

    target = None
    try:
        target = sqlite3.connect(str(temporary), timeout=30)
        db.backup(target)
        target_check = target.execute("PRAGMA integrity_check").fetchone()
        if not target_check or target_check[0] != "ok":
            raise RuntimeError("SQLite backup failed integrity_check")
        target.close()
        target = None
        os.replace(temporary, destination)
    except Exception:
        if target is not None:
            target.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    backups = sorted(
        directory.glob("crypto_ai_trader_*.sqlite3"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for obsolete in backups[keep:]:
        obsolete.unlink(missing_ok=True)
    return str(destination)


def get_backup_health(backup_dir, *, max_age_hours=26, keep=7):
    """Inspect the newest SQLite backup without modifying any files."""
    if isinstance(max_age_hours, bool) or not isinstance(max_age_hours, (int, float)) or not math.isfinite(float(max_age_hours)) or max_age_hours <= 0:
        raise ValueError("max_age_hours must be a positive finite number")
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer")
    directory = Path(backup_dir).expanduser()
    backups = sorted(
        directory.glob("crypto_ai_trader_*.sqlite3") if directory.exists() else [],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return {"healthy": False, "count": 0, "latest_path": None,
                "latest_at": None, "age_seconds": None, "integrity": "missing",
                "retention_ok": True}
    latest = backups[0]
    modified = datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc)
    age_seconds = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds())
    integrity = "error"
    connection = None
    try:
        connection = sqlite3.connect(f"file:{latest}?mode=ro", uri=True, timeout=10)
        row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(row[0]) if row else "no_result"
    except (sqlite3.Error, OSError) as exc:
        integrity = type(exc).__name__
    finally:
        if connection is not None:
            connection.close()
    retention_ok = len(backups) <= keep
    healthy = integrity == "ok" and age_seconds <= float(max_age_hours) * 3600 and retention_ok
    return {"healthy": healthy, "count": len(backups), "latest_path": str(latest),
            "latest_at": modified.isoformat(), "age_seconds": age_seconds,
            "integrity": integrity, "retention_ok": retention_ok}


def audit(db, event, details=""):
    with db:
        db.execute("INSERT INTO audit(event,details) VALUES(?,?)", (event, str(details)))


def save_paper_fx_conversion(db, *, from_currency, to_currency, spent, received,
                             fee_amount=None, fee_currency=None, fee_eur=None, legs=1):
    """Persist a PAPER FX conversion and its independently valued fee."""
    source = str(from_currency or "").strip().upper()
    target = str(to_currency or "").strip().upper()
    currency = None if fee_currency is None else str(fee_currency).strip().upper()
    if not source or not target or source == target:
        raise ValueError("PAPER FX conversion currencies are invalid")
    values = (float(spent), float(received))
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ValueError("PAPER FX conversion amounts must be finite and positive")
    if fee_amount is not None:
        fee_amount = float(fee_amount)
        if not math.isfinite(fee_amount) or fee_amount < 0:
            raise ValueError("PAPER FX fee amount must be finite and non-negative")
    if fee_eur is not None:
        fee_eur = float(fee_eur)
        if not math.isfinite(fee_eur) or fee_eur < 0:
            raise ValueError("PAPER FX EUR fee must be finite and non-negative")
    if isinstance(legs, bool) or not isinstance(legs, int) or legs < 1:
        raise ValueError("PAPER FX conversion legs must be a positive integer")
    with db:
        db.execute(
            "INSERT INTO paper_fx_conversions"
            "(from_currency,to_currency,spent,received,fee_amount,fee_currency,fee_eur,legs) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (source, target, values[0], values[1], fee_amount, currency, fee_eur, legs),
        )

def save_trade(db, mode, symbol, side, qty, quote, fee, price, status, order_id=None, *, fee_eur=None, realized_pnl_eur=None):
    """Persist a PAPER fill with optional EUR-denominated accounting fields."""
    normalized_fee_eur = None if fee_eur is None else float(fee_eur)
    normalized_pnl_eur = None if realized_pnl_eur is None else float(realized_pnl_eur)
    if normalized_fee_eur is not None and (not math.isfinite(normalized_fee_eur) or normalized_fee_eur < 0):
        raise ValueError("EUR fee must be finite and non-negative")
    if normalized_pnl_eur is not None and not math.isfinite(normalized_pnl_eur):
        raise ValueError("Realized EUR PnL must be finite")
    with db:
        db.execute(
            "INSERT INTO trades(mode,symbol,side,qty,quote,fee,price,status,order_id,fee_eur,realized_pnl_eur) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mode, symbol, side, qty, quote, fee, price, status, order_id, normalized_fee_eur, normalized_pnl_eur),
        )

def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        value = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError) as exc:
        if key == "paper_broker":
            raise ValueError("Persisted PAPER wallet state contains invalid JSON") from exc
        return default
    # The virtual wallet is financial state: malformed or non-object JSON must
    # stop startup rather than silently resetting the balance to its initial value.
    if key == "paper_broker" and not isinstance(value, dict):
        raise ValueError("Persisted PAPER wallet state must be a JSON object")
    return value

def set_state(db, key, value):
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False)
    with db:
        db.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", (key, payload))



def save_paper_snapshot(db, *, ts, equity_eur, cash, open_positions, analyzed_markets, buy_signals):
    """Persist one successful PAPER cycle for long-running forward-test reports."""
    equity = float(equity_eur)
    if not math.isfinite(equity) or equity < 0:
        raise ValueError("PAPER snapshot equity must be finite and non-negative")
    if not isinstance(cash, dict):
        raise ValueError("PAPER snapshot cash must be a mapping")
    normalized_cash = {}
    for currency, amount in cash.items():
        if not isinstance(currency, str) or not currency.strip():
            raise ValueError("PAPER snapshot currency must be non-empty")
        value = float(amount)
        if not math.isfinite(value) or value < 0:
            raise ValueError("PAPER snapshot balances must be finite and non-negative")
        normalized_cash[currency.upper()] = value
    counts = (open_positions, analyzed_markets, buy_signals)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError("PAPER snapshot counts must be non-negative integers")
    if not str(ts).strip():
        raise ValueError("PAPER snapshot timestamp must not be empty")
    payload = json.dumps(normalized_cash, separators=(",", ":"), allow_nan=False)
    with db:
        db.execute(
            "INSERT INTO paper_snapshots(ts,equity_eur,cash_json,open_positions,analyzed_markets,buy_signals) "
            "VALUES(?,?,?,?,?,?)",
            (str(ts), equity, payload, open_positions, analyzed_markets, buy_signals),
        )
        # Keep 90 days of minute-level telemetry; never grow SQLite without bound.
        db.execute("DELETE FROM paper_snapshots WHERE julianday(ts) < julianday('now','-90 days')")


def get_latest_paper_snapshot(db):
    """Return the newest saved PAPER valuation, including its source timestamp."""
    row = db.execute(
        "SELECT ts,equity_eur,cash_json,open_positions,analyzed_markets,buy_signals "
        "FROM paper_snapshots ORDER BY julianday(ts) DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "ts": row["ts"],
        "equity_eur": float(row["equity_eur"]),
        "cash": json.loads(row["cash_json"]),
        "open_positions": int(row["open_positions"]),
        "analyzed_markets": int(row["analyzed_markets"]),
        "buy_signals": int(row["buy_signals"]),
    }


def get_paper_metrics(db, hours=24):
    """Summarize forward PAPER equity and realized trade accounting."""
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1 or hours > 2160:
        raise ValueError("hours must be an integer between 1 and 2160")
    rows = db.execute(
        "SELECT ts,equity_eur,open_positions,analyzed_markets,buy_signals "
        "FROM paper_snapshots WHERE julianday(ts) >= julianday('now', ?) ORDER BY ts ASC",
        (f"-{int(hours)} hours",),
    ).fetchall()
    trades = db.execute(
        "SELECT symbol,side,fee_eur,realized_pnl_eur FROM trades "
        "WHERE mode='PAPER' AND status='FILLED' AND julianday(ts) >= julianday('now', ?) ORDER BY ts ASC",
        (f"-{int(hours)} hours",),
    ).fetchall()
    fx_rows = db.execute(
        "SELECT fee_eur FROM paper_fx_conversions "
        "WHERE julianday(ts) >= julianday('now', ?) ORDER BY ts ASC",
        (f"-{int(hours)} hours",),
    ).fetchall()
    sell_rows = [row for row in trades if row["side"].lower() == "sell"]
    pnl_values = [float(row["realized_pnl_eur"]) for row in sell_rows if row["realized_pnl_eur"] is not None]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    by_symbol = {}
    for row in trades:
        if row["side"].lower() != "sell" or row["realized_pnl_eur"] is None:
            continue
        symbol = str(row["symbol"])
        item = by_symbol.setdefault(symbol, {"symbol": symbol, "closed_trades": 0, "realized_pnl_eur": 0.0, "wins": 0})
        value = float(row["realized_pnl_eur"])
        item["closed_trades"] += 1
        item["realized_pnl_eur"] += value
        item["wins"] += int(value > 0)
    valued_closed_trades = len(pnl_values)
    unvalued_closed_trades = sum(row["realized_pnl_eur"] is None for row in sell_rows)
    trade_metrics = {
        "closed_trades": len(sell_rows),
        "closed_trades_with_eur_valuation": valued_closed_trades,
        "closed_trades_without_eur_valuation": unvalued_closed_trades,
        "fills_without_eur_fee_valuation": sum(row["fee_eur"] is None for row in trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": len(wins) / len(pnl_values) * 100 if pnl_values else 0.0,
        "realized_pnl_eur": sum(pnl_values),
        "profit_factor": gross_profit / gross_loss if gross_loss else ("INF" if gross_profit else 0.0),
        "total_fees_eur": sum(float(row["fee_eur"]) for row in trades if row["fee_eur"] is not None),
        "fx_conversions": len(fx_rows),
        "total_fx_conversion_fees_eur": sum(float(row["fee_eur"]) for row in fx_rows if row["fee_eur"] is not None),
        "fx_conversions_without_eur_fee_valuation": sum(row["fee_eur"] is None for row in fx_rows),
        "by_symbol": sorted(by_symbol.values(), key=lambda item: item["realized_pnl_eur"], reverse=True),
    }
    if not rows:
        return {
            "samples": 0, "hours": int(hours),
            "measurement_span_seconds": 0.0,
            "measurement_span_hours": 0.0,
            **trade_metrics,
        }
    def parse_snapshot_timestamp(value):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    first_ts = parse_snapshot_timestamp(rows[0]["ts"])
    last_ts = parse_snapshot_timestamp(rows[-1]["ts"])
    measurement_span_seconds = max(0.0, (last_ts - first_ts).total_seconds())
    first, last = float(rows[0]["equity_eur"]), float(rows[-1]["equity_eur"])
    peak, drawdown = first, 0.0
    for row in rows:
        equity = float(row["equity_eur"])
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)
    return {
        "samples": len(rows), "hours": int(hours),
        "measurement_span_seconds": measurement_span_seconds,
        "measurement_span_hours": measurement_span_seconds / 3600.0,
        "start_equity_eur": first, "end_equity_eur": last,
        "change_eur": last - first,
        "change_pct": (last / first - 1.0) * 100 if first else 0.0,
        "max_observed_drawdown_pct": drawdown * 100,
        "avg_open_positions": sum(int(row["open_positions"]) for row in rows) / len(rows),
        "buy_signals": sum(int(row["buy_signals"]) for row in rows),
        "avg_analyzed_markets": sum(int(row["analyzed_markets"]) for row in rows) / len(rows),
        "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"],
        **trade_metrics,
    }



def record_shadow_prediction(db, *, symbol, model_version='', signal_ts, target_ts, probability, threshold, entry_price):
    """Record one non-overlapping model prediction; never affects the trading engine."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Shadow prediction symbol must not be empty")
    symbol = symbol.strip().upper()
    if not isinstance(model_version, str) or len(model_version) > 128:
        raise ValueError("Shadow model version must be a string of at most 128 characters")
    if any(isinstance(value, bool) for value in (signal_ts, target_ts, probability, threshold, entry_price)):
        raise ValueError("Shadow prediction values must be numeric, not booleans")
    signal_ts, target_ts = float(signal_ts), float(target_ts)
    probability, threshold, entry_price = float(probability), float(threshold), float(entry_price)
    if not all(math.isfinite(x) for x in (signal_ts, target_ts, probability, threshold, entry_price)):
        raise ValueError("Shadow prediction values must be finite")
    if signal_ts < 0 or target_ts <= signal_ts or not 0 <= probability <= 1 or not 0 <= threshold <= 1 or entry_price <= 0:
        raise ValueError("Invalid shadow prediction values")
    with db:
        latest = db.execute(
            "SELECT signal_ts,target_ts FROM ml_shadow_predictions WHERE symbol=? ORDER BY signal_ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if latest and (signal_ts <= float(latest["signal_ts"]) or signal_ts < float(latest["target_ts"])):
            return False
        db.execute(
            "INSERT OR IGNORE INTO ml_shadow_predictions(symbol,model_version,signal_ts,target_ts,probability,threshold,entry_price) "
            "VALUES(?,?,?,?,?,?,?)",
            (symbol, str(model_version), signal_ts, target_ts, probability, threshold, entry_price),
        )
        # Keep 90 days of settled model telemetry; preserve unresolved rows so
        # a temporarily unavailable market feed cannot erase pending outcomes.
        db.execute(
            "DELETE FROM ml_shadow_predictions "
            "WHERE realized_return IS NOT NULL "
            "AND signal_ts < CAST(strftime('%s','now') AS REAL) - 7776000"
        )
    return True


def settle_shadow_predictions(db, *, symbol, candles, fee_rate, slippage_bps, interval_seconds=0):
    """Settle at the first candle close occurring at or after the target time.

    Kraken OHLC timestamps mark candle opens, so callers pass the candle duration
    to avoid waiting an extra full candle after the requested horizon.
    """
    if isinstance(interval_seconds, bool):
        raise ValueError("Shadow candle interval must be a finite non-negative number")
    try:
        interval_seconds = float(interval_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Shadow candle interval must be a finite non-negative number") from exc
    if not math.isfinite(interval_seconds) or interval_seconds < 0:
        raise ValueError("Shadow candle interval must be a finite non-negative number")
    if not isinstance(candles, (list, tuple)):
        raise ValueError("Shadow settlement candles must be a sequence")
    rows = []
    previous_ts = None
    for index, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError(f"Invalid shadow settlement candle at index {index}")
        try:
            timestamp, high, low, close = (float(row[j]) for j in (0, 2, 3, 4))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid shadow settlement candle at index {index}") from exc
        if not all(math.isfinite(value) for value in (timestamp, high, low, close)):
            raise ValueError(f"Non-finite shadow settlement candle at index {index}")
        if timestamp < 0 or min(high, low, close) <= 0 or low > high:
            raise ValueError(f"Invalid shadow settlement OHLC at index {index}")
        if previous_ts is not None and timestamp <= previous_ts:
            raise ValueError("Shadow settlement candles must be strictly chronological")
        previous_ts = timestamp
        rows.append((timestamp, high, low, close))
    if not rows:
        return 0
    fee, slip = float(fee_rate), float(slippage_bps) / 10000.0
    if not math.isfinite(fee) or not math.isfinite(slip) or fee < 0 or fee >= 0.1 or slip < 0 or slip >= 0.1:
        raise ValueError("Invalid shadow settlement fee or slippage")
    settled = 0
    with db:
        pending = db.execute(
            "SELECT id,target_ts,entry_price FROM ml_shadow_predictions "
            "WHERE symbol=? AND realized_return IS NULL AND target_ts<=? ORDER BY target_ts",
            (str(symbol).upper(), float(rows[-1][0])),
        ).fetchall()
        for prediction in pending:
            target = float(prediction["target_ts"])
            exit_row = next((row for row in rows if row[0] + interval_seconds >= target), None)
            if exit_row is None:
                continue
            exit_price = exit_row[3]
            entry_price = float(prediction["entry_price"])
            net_return = (exit_price * (1.0 - slip) * (1.0 - fee)) / (entry_price * (1.0 + slip) * (1.0 + fee)) - 1.0
            db.execute(
                "UPDATE ml_shadow_predictions SET exit_price=?,realized_return=? WHERE id=? AND realized_return IS NULL",
                (exit_price, net_return, prediction["id"]),
            )
            settled += 1
    return settled


def get_shadow_metrics(db, symbol, model_version=None):
    query = "SELECT probability,threshold,realized_return FROM ml_shadow_predictions WHERE symbol=? AND realized_return IS NOT NULL"
    params = [str(symbol).upper()]
    if model_version is not None:
        query += " AND model_version=?"
        params.append(str(model_version))
    query += " ORDER BY signal_ts"
    rows = db.execute(query, tuple(params)).fetchall()
    selected = [float(row["realized_return"]) for row in rows if float(row["probability"]) >= float(row["threshold"])]
    wins = [value for value in selected if value > 0]
    losses = [value for value in selected if value <= 0]
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for value in selected:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak if peak else 0.0)
    gross_loss = abs(sum(losses))
    return {
        "resolved_predictions": len(rows), "shadow_trades": len(selected),
        "wins": len(wins), "net_return_pct": (equity - 1.0) * 100,
        "win_rate_pct": len(wins) / len(selected) * 100 if selected else 0.0,
        "profit_factor": sum(wins) / gross_loss if gross_loss else ("INF" if wins else 0.0),
        "max_drawdown_pct": drawdown * 100,
        "pending": db.execute(
            "SELECT COUNT(*) AS n FROM ml_shadow_predictions WHERE symbol=? AND realized_return IS NULL"
            + (" AND model_version=?" if model_version is not None else ""),
            (str(symbol).upper(), str(model_version)) if model_version is not None else (str(symbol).upper(),),
        ).fetchone()["n"],
    }




def record_strategy_shadow_prediction(db, *, symbol, strategy, signal_ts, target_ts, entry_price, regime=""):
    """Persist one BUY opinion per strategy and closed-candle timestamp."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Strategy shadow symbol must not be empty")
    if not isinstance(strategy, str) or not strategy.strip() or len(strategy) > 64:
        raise ValueError("Strategy shadow name must be a non-empty string of at most 64 characters")
    if not isinstance(regime, str) or len(regime) > 64:
        raise ValueError("Strategy shadow regime must be a string of at most 64 characters")
    values = (signal_ts, target_ts, entry_price)
    if any(isinstance(value, bool) for value in values):
        raise ValueError("Strategy shadow values must be numeric, not booleans")
    try:
        signal_ts, target_ts, entry_price = (float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid strategy shadow values") from exc
    if (not all(math.isfinite(value) for value in (signal_ts, target_ts, entry_price))
            or signal_ts < 0 or target_ts <= signal_ts or entry_price <= 0):
        raise ValueError("Invalid strategy shadow prediction values")
    with db:
        normalized_symbol = symbol.strip().upper()
        normalized_strategy = strategy.strip().lower()
        latest = db.execute(
            "SELECT target_ts FROM strategy_shadow_predictions "
            "WHERE symbol=? AND strategy=? ORDER BY signal_ts DESC LIMIT 1",
            (normalized_symbol, normalized_strategy),
        ).fetchone()
        # Do not compound overlapping three-hour outcomes as if they were
        # independent, fully reinvested trades.
        if latest and signal_ts < float(latest["target_ts"]):
            return False
        cursor = db.execute(
            "INSERT OR IGNORE INTO strategy_shadow_predictions"
            "(symbol,strategy,signal_ts,target_ts,entry_price,regime) VALUES(?,?,?,?,?,?)",
            (normalized_symbol, normalized_strategy, signal_ts, target_ts,
             entry_price, regime),
        )
        db.execute(
            "DELETE FROM strategy_shadow_predictions WHERE realized_return IS NOT NULL "
            "AND signal_ts < CAST(strftime('%s','now') AS REAL) - 7776000"
        )
    return cursor.rowcount == 1


def settle_strategy_shadow_predictions(db, *, symbol, candles, fee_rate, slippage_bps, interval_seconds=3600):
    """Settle strategy shadow BUY opinions at the first due candle close, net of costs."""
    if isinstance(interval_seconds, bool):
        raise ValueError("Strategy shadow candle interval must be numeric")
    try:
        interval_seconds = float(interval_seconds)
        fee, slip = float(fee_rate), float(slippage_bps) / 10000.0
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid strategy shadow settlement costs or interval") from exc
    if (not math.isfinite(interval_seconds) or interval_seconds < 0
            or not math.isfinite(fee) or not math.isfinite(slip)
            or fee < 0 or fee >= 0.1 or slip < 0 or slip >= 0.1):
        raise ValueError("Invalid strategy shadow settlement costs or interval")
    if not isinstance(candles, (list, tuple)):
        raise ValueError("Strategy shadow candles must be a sequence")
    rows, previous_ts = [], None
    for index, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError(f"Invalid strategy shadow candle at index {index}")
        try:
            timestamp, high, low, close = (float(row[j]) for j in (0, 2, 3, 4))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid strategy shadow candle at index {index}") from exc
        if (not all(math.isfinite(value) for value in (timestamp, high, low, close))
                or timestamp < 0 or min(high, low, close) <= 0 or low > high
                or (previous_ts is not None and timestamp <= previous_ts)):
            raise ValueError(f"Invalid strategy shadow candle at index {index}")
        previous_ts = timestamp
        rows.append((timestamp, close))
    if not rows:
        return 0
    settled = 0
    with db:
        pending = db.execute(
            "SELECT id,target_ts,entry_price FROM strategy_shadow_predictions "
            "WHERE symbol=? AND realized_return IS NULL AND target_ts<=? ORDER BY target_ts",
            (str(symbol).upper(), rows[-1][0] + interval_seconds),
        ).fetchall()
        for prediction in pending:
            target = float(prediction["target_ts"])
            exit_row = next((row for row in rows if row[0] + interval_seconds >= target), None)
            if exit_row is None:
                continue
            exit_price = exit_row[1]
            entry_price = float(prediction["entry_price"])
            net_return = (
                exit_price * (1.0 - slip) * (1.0 - fee)
                / (entry_price * (1.0 + slip) * (1.0 + fee))
            ) - 1.0
            db.execute(
                "UPDATE strategy_shadow_predictions SET exit_price=?,realized_return=? "
                "WHERE id=? AND realized_return IS NULL",
                (exit_price, net_return, prediction["id"]),
            )
            settled += 1
    return settled


def get_strategy_shadow_metrics(db, symbol=None):
    """Return net-of-cost performance independently for every shadow strategy."""
    query = (
        "SELECT strategy,realized_return FROM strategy_shadow_predictions "
        "WHERE realized_return IS NOT NULL"
    )
    params = []
    if symbol is not None:
        query += " AND symbol=?"
        params.append(str(symbol).upper())
    query += " ORDER BY strategy,signal_ts"
    rows = db.execute(query, tuple(params)).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["strategy"], []).append(float(row["realized_return"]))
    output = {}
    for strategy, returns in grouped.items():
        equity, peak, drawdown = 1.0, 1.0, 0.0
        wins, losses = [], []
        for value in returns:
            equity *= 1.0 + value
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak if peak else 0.0)
            (wins if value > 0 else losses).append(value)
        gross_loss = abs(sum(losses))
        output[strategy] = {
            "resolved": len(returns),
            "wins": len(wins),
            "win_rate_pct": len(wins) / len(returns) * 100.0,
            "net_return_pct": (equity - 1.0) * 100.0,
            "profit_factor": sum(wins) / gross_loss if gross_loss else ("INF" if wins else 0.0),
            "max_drawdown_pct": drawdown * 100.0,
        }
    return output




def get_strategy_shadow_regime_metrics(db, symbol=None):
    """Return net-of-cost strategy metrics grouped by market regime."""
    query = (
        "SELECT strategy,regime,realized_return FROM strategy_shadow_predictions "
        "WHERE realized_return IS NOT NULL"
    )
    params = []
    if symbol is not None:
        query += " AND symbol=?"
        params.append(str(symbol).upper())
    query += " ORDER BY regime,strategy,signal_ts"
    grouped = {}
    for row in db.execute(query, tuple(params)).fetchall():
        key = (row["regime"] or "UNKNOWN", row["strategy"])
        grouped.setdefault(key, []).append(float(row["realized_return"]))
    output = {}
    for (regime, strategy), returns in grouped.items():
        equity = peak = 1.0
        drawdown = 0.0
        wins, losses = [], []
        for value in returns:
            equity *= 1.0 + value
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak if peak else 0.0)
            (wins if value > 0 else losses).append(value)
        gross_loss = abs(sum(losses))
        output.setdefault(regime, {})[strategy] = {
            "resolved": len(returns),
            "win_rate_pct": len(wins) / len(returns) * 100.0,
            "net_return_pct": (equity - 1.0) * 100.0,
            "profit_factor": sum(wins) / gross_loss if gross_loss else ("INF" if wins else 0.0),
            "max_drawdown_pct": drawdown * 100.0,
        }
    return output


def save_telegram_report_pages(db, report_key, pages, *, keep=100):
    """Persist Telegram report pages so inline navigation survives process restarts."""
    if not isinstance(report_key, str) or not report_key.strip() or len(report_key) > 128:
        raise ValueError("Telegram report key must be a non-empty string of at most 128 characters")
    if not isinstance(pages, (list, tuple)) or not pages or any(not isinstance(page, str) for page in pages):
        raise ValueError("Telegram report pages must be a non-empty sequence of strings")
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("Telegram report retention must be a positive integer")
    payload = json.dumps(list(pages), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with db:
        db.execute(
            "INSERT INTO telegram_report_pages(report_key,pages_json,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(report_key) DO UPDATE SET pages_json=excluded.pages_json,updated_at=CURRENT_TIMESTAMP",
            (report_key, payload),
        )
        db.execute(
            "DELETE FROM telegram_report_pages WHERE report_key IN ("
            "SELECT report_key FROM telegram_report_pages ORDER BY updated_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (keep,),
        )


def get_telegram_report_pages(db, report_key):
    """Load persisted Telegram report pages, returning None for expired/unknown keys."""
    if not isinstance(report_key, str) or not report_key.strip() or len(report_key) > 128:
        return None
    row = db.execute(
        "SELECT pages_json FROM telegram_report_pages WHERE report_key=?", (report_key,)
    ).fetchone()
    if row is None:
        return None
    try:
        pages = json.loads(row["pages_json"])
    except (TypeError, json.JSONDecodeError):
        with db:
            db.execute("DELETE FROM telegram_report_pages WHERE report_key=?", (report_key,))
        return None
    if not isinstance(pages, list) or not pages or any(not isinstance(page, str) for page in pages):
        with db:
            db.execute("DELETE FROM telegram_report_pages WHERE report_key=?", (report_key,))
        return None
    return pages


def save_paper_fill(db, *, cash_by_currency, positions, symbol, side, qty, quote, fee, price,
                    fee_eur=None, realized_pnl_eur=None):
    """Atomically persist a PAPER wallet mutation and its corresponding trade fill."""
    if not isinstance(cash_by_currency, dict) or not isinstance(positions, dict):
        raise ValueError("PAPER wallet state must contain currency balances and positions")
    normalized_fee_eur = None if fee_eur is None else float(fee_eur)
    normalized_pnl_eur = None if realized_pnl_eur is None else float(realized_pnl_eur)
    if normalized_fee_eur is not None and (not math.isfinite(normalized_fee_eur) or normalized_fee_eur < 0):
        raise ValueError("EUR fee must be finite and non-negative")
    if normalized_pnl_eur is not None and not math.isfinite(normalized_pnl_eur):
        raise ValueError("Realized EUR PnL must be finite")
    wallet = {
        "cash": float(cash_by_currency.get("EUR", 0.0)),
        "cash_by_currency": cash_by_currency,
        "positions": positions,
    }
    payload = json.dumps(wallet, separators=(",", ":"), allow_nan=False)
    numeric = (float(qty), float(quote), float(fee), float(price))
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("PAPER fill values must be finite")
    if any(value < 0 for value in numeric) or numeric[0] <= 0 or numeric[3] <= 0:
        raise ValueError("PAPER fill values are outside the valid range")
    with db:
        db.execute(
            "INSERT INTO app_state(key,value,updated_at) VALUES('paper_broker',?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
            (payload,),
        )
        db.execute(
            "INSERT INTO trades(mode,symbol,side,qty,quote,fee,price,status,fee_eur,realized_pnl_eur) "
            "VALUES('PAPER',?,?,?,?,?,?, 'FILLED',?,?)",
            (str(symbol), str(side), numeric[0], numeric[1], numeric[2], numeric[3],
             normalized_fee_eur, normalized_pnl_eur),
        )


def _account_number(value, field, *, positive=False, non_negative=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid Kraken {field}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Invalid Kraken {field}")
    if positive and number <= 0:
        raise ValueError(f"Kraken {field} must be positive")
    if non_negative and number < 0:
        raise ValueError(f"Kraken {field} must not be negative")
    return number


def import_kraken_account_history(
    db, *, trades, ledgers, balances, fetched_at,
    fills_complete, ledger_complete,
):
    """Idempotently import a fully fetched Kraken read-only account snapshot.

    Raw exchange records are retained for audit. This does not calculate P/L:
    cost-basis and EUR conversion must be independently established first.
    """
    if not isinstance(trades, (list, tuple)) or not isinstance(ledgers, (list, tuple)):
        raise ValueError("Kraken histories must be lists")
    if not isinstance(balances, dict):
        raise ValueError("Kraken balances must be an object")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise ValueError("fetched_at must be a non-empty timestamp")
    if not isinstance(fills_complete, bool) or not isinstance(ledger_complete, bool):
        raise ValueError("History completeness flags must be booleans")

    normalized_trades = []
    seen_trade_ids = set()
    for item in trades:
        if not isinstance(item, dict):
            raise ValueError("Malformed Kraken trade record")
        trade_id = str(item.get("id") or "").strip()
        pair = str(item.get("pair") or "").strip().upper()
        side = str(item.get("type") or "").strip().lower()
        if not trade_id or not pair or side not in {"buy", "sell"}:
            raise ValueError("Kraken trade is missing id, pair, or valid side")
        if trade_id in seen_trade_ids:
            raise ValueError(f"Duplicate Kraken trade id in fetched history: {trade_id}")
        seen_trade_ids.add(trade_id)
        normalized_trades.append((
            trade_id, str(item.get("ordertxid") or "") or None, pair,
            _account_number(item.get("time"), "trade time", non_negative=True),
            side, str(item.get("ordertype") or "") or None,
            _account_number(item.get("price"), "trade price", positive=True),
            _account_number(item.get("cost"), "trade cost", non_negative=True),
            _account_number(item.get("fee"), "trade fee", non_negative=True),
            _account_number(item.get("vol"), "trade volume", positive=True),
            json.dumps(item, separators=(",", ":"), allow_nan=False, sort_keys=True),
        ))

    normalized_ledgers = []
    seen_ledger_ids = set()
    for item in ledgers:
        if not isinstance(item, dict):
            raise ValueError("Malformed Kraken ledger record")
        ledger_id = str(item.get("id") or "").strip()
        asset = str(item.get("asset") or "").strip().upper()
        entry_type = str(item.get("type") or "").strip().lower()
        if not ledger_id or not asset or not entry_type:
            raise ValueError("Kraken ledger record is missing id, asset, or type")
        if ledger_id in seen_ledger_ids:
            raise ValueError(f"Duplicate Kraken ledger id in fetched history: {ledger_id}")
        seen_ledger_ids.add(ledger_id)
        raw_balance = item.get("balance")
        balance = None if raw_balance is None else _account_number(
            raw_balance, "ledger balance", non_negative=True
        )
        normalized_ledgers.append((
            ledger_id, str(item.get("refid") or "") or None,
            _account_number(item.get("time"), "ledger time", non_negative=True),
            entry_type, str(item.get("subtype") or "") or None, asset,
            _account_number(item.get("amount"), "ledger amount"),
            _account_number(item.get("fee", 0), "ledger fee", non_negative=True),
            balance,
            json.dumps(item, separators=(",", ":"), allow_nan=False, sort_keys=True),
        ))

    normalized_balances = {}
    for currency, amount in balances.items():
        code = str(currency or "").strip().upper()
        if not code:
            raise ValueError("Kraken balance currency is empty")
        normalized_balances[code] = _account_number(
            amount, f"{code} balance", non_negative=True
        )
    balance_json = json.dumps(normalized_balances, separators=(",", ":"), allow_nan=False, sort_keys=True)

    # A repeated exchange identifier is idempotent only when its immutable
    # payload matches. A conflicting payload is a data-integrity failure.
    for row in normalized_trades:
        existing = db.execute(
            "SELECT raw_json FROM kraken_fills WHERE trade_id=?", (row[0],)
        ).fetchone()
        if existing is not None and existing["raw_json"] != row[10]:
            raise ValueError(f"Conflicting Kraken trade payload for id {row[0]}")
    for row in normalized_ledgers:
        existing = db.execute(
            "SELECT raw_json FROM kraken_ledger_entries WHERE ledger_id=?", (row[0],)
        ).fetchone()
        if existing is not None and existing["raw_json"] != row[9]:
            raise ValueError(f"Conflicting Kraken ledger payload for id {row[0]}")

    with db:
        before_fills = db.execute("SELECT COUNT(*) FROM kraken_fills").fetchone()[0]
        before_ledgers = db.execute("SELECT COUNT(*) FROM kraken_ledger_entries").fetchone()[0]
        db.executemany(
            "INSERT OR IGNORE INTO kraken_fills"
            "(trade_id,order_id,pair,time,side,order_type,price,cost,fee,volume,raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            normalized_trades,
        )
        db.executemany(
            "INSERT OR IGNORE INTO kraken_ledger_entries"
            "(ledger_id,refid,time,entry_type,subtype,asset,amount,fee,balance,raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            normalized_ledgers,
        )
        after_fills = db.execute("SELECT COUNT(*) FROM kraken_fills").fetchone()[0]
        after_ledgers = db.execute("SELECT COUNT(*) FROM kraken_ledger_entries").fetchone()[0]
        imported_fills = after_fills - before_fills
        imported_ledgers = after_ledgers - before_ledgers
        status = "COMPLETE" if fills_complete and ledger_complete else "INCOMPLETE"
        db.execute(
            "INSERT INTO kraken_account_sync"
            "(id,fetched_at,fills_complete,ledger_complete,exchange_fill_count,imported_fill_count,"
            "exchange_ledger_count,imported_ledger_count,balance_json,status) "
            "VALUES(1,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET fetched_at=excluded.fetched_at,"
            "fills_complete=excluded.fills_complete,ledger_complete=excluded.ledger_complete,"
            "exchange_fill_count=excluded.exchange_fill_count,imported_fill_count=excluded.imported_fill_count,"
            "exchange_ledger_count=excluded.exchange_ledger_count,imported_ledger_count=excluded.imported_ledger_count,"
            "balance_json=excluded.balance_json,status=excluded.status",
            (fetched_at, int(fills_complete), int(ledger_complete), len(trades),
             imported_fills, len(ledgers), imported_ledgers, balance_json, status),
        )
    return {
        "status": status,
        "exchange_fill_count": len(trades),
        "new_fills_imported": imported_fills,
        "exchange_ledger_count": len(ledgers),
        "new_ledger_entries_imported": imported_ledgers,
        "balance": normalized_balances,
    }


def get_kraken_account_sync(db):
    """Return last raw account import status; never label missing data as zero P/L."""
    row = db.execute("SELECT * FROM kraken_account_sync WHERE id=1").fetchone()
    if row is None:
        return {"status": "UNKNOWN", "reason": "No Kraken account history has been imported"}
    return {
        "status": row["status"],
        "fetched_at": row["fetched_at"],
        "fills_complete": bool(row["fills_complete"]),
        "ledger_complete": bool(row["ledger_complete"]),
        "exchange_fill_count": int(row["exchange_fill_count"]),
        "exchange_ledger_count": int(row["exchange_ledger_count"]),
        "balance": json.loads(row["balance_json"]),
        "realized_pnl_eur": None,
        "unrealized_pnl_eur": None,
        "pnl_status": "NOT_CALCULATED",
    }
