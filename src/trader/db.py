import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, event TEXT NOT NULL, details TEXT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, quote REAL NOT NULL, fee REAL NOT NULL, price REAL NOT NULL, status TEXT NOT NULL, order_id TEXT);
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
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    # Additive migration for databases created before model-version tracking.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(ml_shadow_predictions)").fetchall()}
    if "model_version" not in columns:
        db.execute("ALTER TABLE ml_shadow_predictions ADD COLUMN model_version TEXT NOT NULL DEFAULT ''")
    return db

def audit(db, event, details=""):
    with db:
        db.execute("INSERT INTO audit(event,details) VALUES(?,?)", (event, str(details)))

def save_trade(db, mode, symbol, side, qty, quote, fee, price, status, order_id=None):
    with db:
        db.execute("INSERT INTO trades(mode,symbol,side,qty,quote,fee,price,status,order_id) VALUES(?,?,?,?,?,?,?,?,?)",
                   (mode, symbol, side, qty, quote, fee, price, status, order_id))

def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    if row is None: return default
    try: return json.loads(row["value"])
    except (TypeError, json.JSONDecodeError): return default

def set_state(db, key, value):
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False)
    with db:
        db.execute("INSERT INTO app_state(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", (key, payload))



def save_paper_snapshot(db, *, ts, equity_eur, cash, open_positions, analyzed_markets, buy_signals):
    """Persist one successful PAPER cycle for long-running forward-test reports."""
    payload = json.dumps(cash, separators=(",", ":"), allow_nan=False)
    with db:
        db.execute(
            "INSERT INTO paper_snapshots(ts,equity_eur,cash_json,open_positions,analyzed_markets,buy_signals) "
            "VALUES(?,?,?,?,?,?)",
            (str(ts), float(equity_eur), payload, int(open_positions), int(analyzed_markets), int(buy_signals)),
        )
        # Keep 90 days of minute-level telemetry; never grow SQLite without bound.
        db.execute("DELETE FROM paper_snapshots WHERE ts < datetime('now','-90 days')")


def get_paper_metrics(db, hours=24):
    """Summarize persisted forward PAPER telemetry for the requested period."""
    if isinstance(hours, bool) or int(hours) < 1 or int(hours) > 2160:
        raise ValueError("hours must be between 1 and 2160")
    rows = db.execute(
        "SELECT ts,equity_eur,open_positions,analyzed_markets,buy_signals "
        "FROM paper_snapshots WHERE ts >= datetime('now', ?) ORDER BY ts ASC",
        (f"-{int(hours)} hours",),
    ).fetchall()
    if not rows:
        return {"samples": 0, "hours": int(hours)}
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
    }



def record_shadow_prediction(db, *, symbol, model_version='', signal_ts, target_ts, probability, threshold, entry_price):
    """Record one non-overlapping model prediction; never affects the trading engine."""
    symbol = str(symbol).upper()
    signal_ts, target_ts = float(signal_ts), float(target_ts)
    probability, threshold, entry_price = float(probability), float(threshold), float(entry_price)
    if not all(__import__("math").isfinite(x) for x in (signal_ts, target_ts, probability, threshold, entry_price)):
        raise ValueError("Shadow prediction values must be finite")
    if target_ts <= signal_ts or not 0 <= probability <= 1 or not 0 <= threshold <= 1 or entry_price <= 0:
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
    return True


def settle_shadow_predictions(db, *, symbol, candles, fee_rate, slippage_bps):
    """Settle due shadow predictions using the first closed candle at/after target time."""
    rows = sorted(candles, key=lambda row: float(row[0]))
    if not rows:
        return 0
    fee, slip = float(fee_rate), float(slippage_bps) / 10000.0
    settled = 0
    with db:
        pending = db.execute(
            "SELECT id,target_ts,entry_price FROM ml_shadow_predictions "
            "WHERE symbol=? AND realized_return IS NULL AND target_ts<=? ORDER BY target_ts",
            (str(symbol).upper(), float(rows[-1][0])),
        ).fetchall()
        for prediction in pending:
            target = float(prediction["target_ts"])
            exit_row = next((row for row in rows if float(row[0]) >= target), None)
            if exit_row is None:
                continue
            exit_price = float(exit_row[4])
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
