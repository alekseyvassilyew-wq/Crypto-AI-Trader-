from types import SimpleNamespace
from src.trader.db import connect, get_state, set_state
from src.trader.engine import Engine
from src.trader.paper import PaperBroker


class DummyExchange:
    def tickers(self, symbols): return []
    def candles(self, symbol): return []


def make_engine(tmp_path):
    settings = SimpleNamespace(
        scan_interval=60, max_trade=5, max_daily_loss=2, max_positions=2,
        max_spread_bps=25, min_turnover=10000, watchlist=("XBTEUR",),
        stop_loss_pct=2, take_profit_pct=4,
    )
    db = connect(tmp_path / "engine.sqlite3")
    paper = PaperBroker(20, .0026, db=db)
    return Engine(settings, db, DummyExchange(), paper), db


def test_stop_pauses_and_audits_without_name_error(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.start()
    engine.scan_interval_seconds = 300
    engine.stop("test stop")
    assert engine.paused is True
    assert engine.scan_interval_seconds == 60
    row = db.execute("SELECT event, details FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["event"] == "PAPER_STOPPED"
    assert row["details"] == "test stop"
    db.close()


def test_new_day_start_equity_includes_open_positions(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 10)
    set_state(db, "daily", {"date": "2000-01-01", "start_equity": 20.0, "realized": 0.0})
    engine._ensure_day()
    daily = get_state(db, "daily")
    assert daily["start_equity"] == engine.paper.equity({})
    db.close()
