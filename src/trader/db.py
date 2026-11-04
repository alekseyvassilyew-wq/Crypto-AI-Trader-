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
