import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, event TEXT NOT NULL, details TEXT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, quote REAL NOT NULL, fee REAL NOT NULL, price REAL NOT NULL, status TEXT NOT NULL, order_id TEXT);
CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
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
