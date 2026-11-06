"""Persistent, auditable history for leakage-aware model research.

All stored candles and research results are local SQLite telemetry. This module
never connects to order execution and never promotes a model.
"""
import json
import math
from datetime import datetime, timezone


def _ensure_schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ml_research_candles (
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        ts REAL NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL NOT NULL,
        PRIMARY KEY(symbol, interval, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_ml_research_candles_ts
        ON ml_research_candles(symbol, interval, ts);
    CREATE TABLE IF NOT EXISTS ml_research_runs (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        model_version TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL,
        outcome TEXT NOT NULL,
        samples INTEGER NOT NULL DEFAULT 0,
        oos_return_pct REAL,
        holdout_return_pct REAL,
        profit_factor TEXT,
        max_drawdown_pct REAL,
        trades INTEGER NOT NULL DEFAULT 0,
        reasons_json TEXT NOT NULL DEFAULT '[]',
        report_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_ml_research_runs_symbol_created
        ON ml_research_runs(symbol, created_at DESC);
    """)


def store_candles(db, symbol, candles, interval="60"):
    """Insert immutable validated closed OHLCV candles; retain up to two years."""
    symbol = str(symbol).strip().upper().replace("/", "")
    if not symbol:
        raise ValueError("Research candle symbol must not be empty")
    normalized = []
    previous = None
    for index, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError(f"Invalid research candle at index {index}")
        values = [float(row[i]) for i in range(6)]
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"Non-finite research candle at index {index}")
        ts, op, high, low, close, volume = values
        if ts < 0 or min(op, high, low, close) <= 0 or volume < 0:
            raise ValueError(f"Invalid research OHLCV at index {index}")
        if low > min(op, close) or high < max(op, close) or low > high:
            raise ValueError(f"Inconsistent research OHLC at index {index}")
        if previous is not None and ts <= previous:
            raise ValueError("Research candles must be strictly chronological")
        previous = ts
        normalized.append((symbol, str(interval), ts, op, high, low, close, volume))
    if not normalized:
        return 0
    _ensure_schema(db)
    with db:
        # Closed historical candles are immutable research inputs. Re-fetching
        # identical rows is idempotent, but silently replacing a previously
        # stored candle would rewrite the evidence behind old model results.
        first_ts, last_ts = normalized[0][2], normalized[-1][2]
        existing_rows = db.execute(
            """SELECT ts,open,high,low,close,volume
               FROM ml_research_candles
               WHERE symbol=? AND interval=? AND ts BETWEEN ? AND ?""",
            (symbol, str(interval), first_ts, last_ts),
        ).fetchall()
        existing = {float(row["ts"]): tuple(float(row[i]) for i in range(1, 6))
                    for row in existing_rows}
        for item in normalized:
            ts = item[2]
            previous_values = existing.get(ts)
            incoming_values = item[3:]
            if previous_values is not None and previous_values != incoming_values:
                raise ValueError(
                    f"Conflicting historical research candle for {symbol} at timestamp {ts}"
                )
        db.executemany(
            """INSERT OR IGNORE INTO ml_research_candles
               (symbol, interval, ts, open, high, low, close, volume)
               VALUES(?,?,?,?,?,?,?,?)""",
            normalized,
        )
        db.execute(
            "DELETE FROM ml_research_candles WHERE ts < ?",
            (datetime.now(timezone.utc).timestamp() - 730 * 86400,),
        )
    return len(normalized)


def load_candles(db, symbol, interval="60"):
    _ensure_schema(db)
    rows = db.execute(
        """SELECT ts,open,high,low,close,volume FROM ml_research_candles
           WHERE symbol=? AND interval=? ORDER BY ts""",
        (str(symbol).strip().upper(), str(interval)),
    ).fetchall()
    return [[float(row[i]) for i in range(6)] for row in rows]


def _report_payload(candidate):
    # The fitted weights remain in app_state for shadow inference. The history
    # table stores metrics and assumptions, not a second copy of model weights.
    return {
        key: value for key, value in candidate.items()
        if key not in {"model"}
    }


def save_research_run(db, candidate, source="automatic"):
    _ensure_schema(db)
    metrics = candidate.get("out_of_sample") or {}
    holdout = candidate.get("independent_holdout") or {}
    reasons = candidate.get("rejection_reasons") or []
    pf = metrics.get("profit_factor")
    report = json.dumps(_report_payload(candidate), separators=(",", ":"), allow_nan=False)
    with db:
        db.execute(
            """INSERT INTO ml_research_runs
               (created_at,symbol,model_version,source,outcome,samples,oos_return_pct,
                holdout_return_pct,profit_factor,max_drawdown_pct,trades,reasons_json,report_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(candidate.get("trained_at_utc") or datetime.now(timezone.utc).isoformat()),
                str(candidate.get("symbol") or "").upper(),
                str(candidate.get("model_version") or ""),
                str(source),
                "PASS" if candidate.get("research_pass") else "NOT_PASS",
                int(candidate.get("samples") or 0),
                float(metrics.get("net_return_pct") or 0.0),
                float(holdout.get("net_return_pct") or 0.0),
                str(pf if pf is not None else "N/A"),
                float(metrics.get("max_drawdown_pct") or 0.0),
                int(metrics.get("trades") or 0),
                json.dumps(reasons, separators=(",", ":"), allow_nan=False),
                report,
            ),
        )
        db.execute(
            "DELETE FROM ml_research_runs WHERE created_at < datetime('now','-730 days')"
        )


def save_research_failure(db, symbol, source, error):
    _ensure_schema(db)
    now = datetime.now(timezone.utc).isoformat()
    reasons = [str(error)[:500]]
    with db:
        db.execute(
            """INSERT INTO ml_research_runs
               (created_at,symbol,source,outcome,reasons_json,report_json)
               VALUES(?,?,?,?,?,?)""",
            (now, str(symbol).strip().upper().replace("/", ""), str(source), "INSUFFICIENT_DATA",
             json.dumps(reasons), json.dumps({"error": reasons[0]})),
        )


def list_research_runs(db, limit=10, symbol=None):
    _ensure_schema(db)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("Research history limit must be between 1 and 50")
    if symbol:
        rows = db.execute(
            """SELECT * FROM ml_research_runs WHERE symbol=?
               ORDER BY id DESC LIMIT ?""",
            (str(symbol).strip().upper(), limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM ml_research_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        for field in ("reasons_json", "report_json"):
            try:
                item[field.replace("_json", "")] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError):
                item[field.replace("_json", "")] = {}
        output.append(item)
    return output


def perform_research(db, exchange, settings, symbol, source="manual"):
    """Fetch, accumulate and evaluate candles; persist both successes and failures."""