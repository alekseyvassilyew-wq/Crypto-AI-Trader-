import json
import math
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, event TEXT NOT NULL, details TEXT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, quote REAL NOT NULL, fee REAL NOT NULL, price REAL NOT NULL, status TEXT NOT NULL, order_id TEXT, fee_eur REAL, realized_pnl_eur REAL);
CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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


def audit(db, event, details=""):
    with db:
        db.execute("INSERT INTO audit(event,details) VALUES(?,?)", (event, str(details)))

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
    trade_metrics = {
        "closed_trades": len(pnl_values),
        "closed_trades_without_eur_valuation": sum(row["realized_pnl_eur"] is None for row in sell_rows),
        "fills_without_eur_fee_valuation": sum(row["fee_eur"] is None for row in trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": len(wins) / len(pnl_values) * 100 if pnl_values else 0.0,
        "realized_pnl_eur": sum(pnl_values),
        "profit_factor": gross_profit / gross_loss if gross_loss else ("INF" if gross_profit else 0.0),
        "total_fees_eur": sum(float(row["fee_eur"]) for row in trades if row["fee_eur"] is not None),
        "by_symbol": sorted(by_symbol.values(), key=lambda item: item["realized_pnl_eur"], reverse=True),
    }
    if not rows:
        return {"samples": 0, "hours": int(hours), **trade_metrics}
    first, last = float(rows[0]["equity_eur"]), float(rows[-1]["equity_eur"])
    peak, drawdown = first, 0.0
    for row in rows:
        equity = float(row["equity_eur"])
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)
    return {
        "samples": len(rows), "hours": int(hours),
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


def settle_shadow_predictions(db, *, symbol, candles, fee_rate, slippage_bps):
    """Settle due shadow predictions using the first closed candle at/after target time."""
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
            exit_row = next((row for row in rows if row[0] >= target), None)
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
