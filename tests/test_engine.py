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


def test_exchange_scan_error_pauses_engine_and_audits(tmp_path):
    engine, db = make_engine(tmp_path)

    class BrokenExchange:
        def tickers(self, symbols):
            raise TimeoutError("Kraken request timed out")

    engine.exchange = BrokenExchange()
    engine.start()
    actions = engine.cycle()

    assert engine.paused is True
    assert actions == ["SCAN_FAILED — paper trading paused"]
    row = db.execute("SELECT event, details FROM audit WHERE event='SCAN_FAILED'").fetchone()
    assert row is not None
    assert "TimeoutError" in row["details"]
    db.close()


def test_daily_loss_limit_pauses_engine(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.start()
    set_state(db, "daily", {
        "date": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat(),
        "start_equity": 30.0,
        "realized": 0.0,
    })

    actions = engine.cycle()

    assert engine.paused is True
    assert actions == ["DAILY LOSS LIMIT — paused"]
    row = db.execute("SELECT event FROM audit WHERE event='PAPER_STOPPED' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["event"] == "PAPER_STOPPED"
    db.close()


def test_engine_starts_paused_after_restart(tmp_path):
    engine, db = make_engine(tmp_path)
    assert engine.paused is True
    assert engine.cycle() == []
    db.close()


def test_full_market_scan_monitors_all_pairs_but_analyzes_only_eur_budget(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.trade_quote_currency = "EUR"
    engine.s.max_candle_pairs = 1

    class FullMarketExchange:
        def discover_pairs(self):
            return [
                {"symbol": "XBTEUR", "quote": "EUR", "status": "online"},
                {"symbol": "ETHEUR", "quote": "EUR", "status": "online"},
                {"symbol": "ETHUSD", "quote": "USD", "status": "online"},
            ]

        def tickers(self):
            return [
                {"symbol": "XBTEUR", "quote": "EUR", "bid1Price": 99.95, "ask1Price": 100, "lastPrice": 99.98, "turnover24h": 50000},
                {"symbol": "ETHEUR", "quote": "EUR", "bid1Price": 49.99, "ask1Price": 50, "lastPrice": 49.995, "turnover24h": 20000},
                {"symbol": "ETHUSD", "quote": "USD", "bid1Price": 2999, "ask1Price": 3001, "lastPrice": 3000, "turnover24h": 500000},
            ]

    engine.exchange = FullMarketExchange()
    analyzed = []

    def fake_scan_one(symbol, tickers=None):
        analyzed.append(symbol)
        return {"symbol": symbol, "signal": "HOLD", "score": 1, "price": 99, "spread_bps": 10, "turnover": 50000, "reasons": []}

    engine.scan_one = fake_scan_one
    rows = engine.scan()

    assert engine.market_ticker_count == 3
    assert len(rows) == 3
    assert analyzed == ["XBTEUR"]
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["ETHUSD"]["signal"] == "HOLD"
    assert "Monitored only" in by_symbol["ETHUSD"]["reasons"][0]
    assert "deferred" in by_symbol["ETHEUR"]["reasons"][0]
    db.close()


def test_rotating_scan_covers_multiple_funded_quote_currencies(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 20
    engine.s.max_candle_pairs = 1
    class MultiQuoteExchange:
        def discover_pairs(self):
            return [
                {"symbol": "XBTEUR", "base": "XBT", "quote": "EUR"},
                {"symbol": "ETHUSD", "base": "ETH", "quote": "USD"},
            ]
        def tickers(self):
            return [
                {"symbol": "XBTEUR", "bid1Price": 99, "ask1Price": 100, "lastPrice": 99.5, "turnover24h": 50000},
                {"symbol": "ETHUSD", "bid1Price": 2990, "ask1Price": 3000, "lastPrice": 2995, "turnover24h": 40000},
            ]
    engine.exchange = MultiQuoteExchange()
    seen = []
    def fake_scan_one(symbol, tickers=None):
        seen.append(symbol)
        return {"symbol": symbol, "signal": "HOLD", "score": 1, "price": 1, "spread_bps": 10, "turnover": 50000, "reasons": []}
    engine.scan_one = fake_scan_one
    first = engine.scan()
    second = engine.scan()
    assert seen == ["XBTEUR", "ETHUSD"]
    assert {row["quote"] for row in first + second} == {"EUR", "USD"}
    db.close()


def test_fx_rates_convert_quote_currencies_to_eur(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.market_pairs = [
        {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
        {"symbol": "ETHUSD", "base": "ETH", "quote": "USD"},
    ]
    rates = engine._fx_rates({"USDZEUR": 0.9, "ETHUSD": 3000})
    assert rates["USD"] == 0.9
    assert rates["ETH"] == 2700
    db.close()