from types import SimpleNamespace
from src.trader.db import connect, get_state, set_state
from src.trader.engine import Engine
from src.trader.telegram_ui import _format_paper_trade_action
from src.trader.paper import PaperBroker
import pytest


class DummyExchange:
    def tickers(self, symbols): return []
    def candles(self, symbol): return []


def make_engine(tmp_path):
    settings = SimpleNamespace(
        scan_interval=60, max_trade=5, max_daily_loss=2, max_positions=2,
        max_spread_bps=25, min_turnover=10000, watchlist=("XBTEUR",),
        stop_loss_pct=2, take_profit_pct=4, paper_slippage_bps=5,
    )
    db = connect(tmp_path / "engine.sqlite3")
    paper = PaperBroker(20, .0026, db=db)
    return Engine(settings, db, DummyExchange(), paper), db


def test_paper_trade_eur_accounting_includes_both_sides_fees_and_fresh_fx(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.fee_rate = 0.01
    engine._fresh_fx_rates = lambda prices: {"EUR": 1.0, "USD": 0.9}

    fee_eur, realized_pnl_eur = engine._paper_trade_eur_values(
        "USD", 0.24, {"ETHUSD": 12.0},
        position={
            "qty": 2.0, "avg_price": 10.0,
            "cost_basis_eur": 18.0, "entry_fees_eur": 0.18,
        }, qty=2.0, exit_price=12.0,
    )

    assert fee_eur == pytest.approx(0.216)
    assert realized_pnl_eur == pytest.approx(3.204)
    db.close()


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
    fresh_prices = {"XBTEUR": 12.0}
    engine._ensure_day(fresh_prices)
    daily = get_state(db, "daily")
    assert daily["start_equity"] == engine._equity_after_liquidation(fresh_prices, engine._fx_rates(fresh_prices))
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


def test_fresh_engine_starts_running_and_manual_pause_survives_restart(tmp_path):
    engine, db = make_engine(tmp_path)
    assert engine.paused is False

    engine.stop("user requested pause")
    restarted = Engine(engine.s, db, DummyExchange(), engine.paper)
    assert restarted.paused is True

    restarted.start()
    assert restarted.paused is False
    restarted_again = Engine(engine.s, db, DummyExchange(), engine.paper)
    assert restarted_again.paused is False
    db.close()


def test_engine_keeps_monitoring_without_opening_positions_when_signals_hold(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 1,
        "price": 10.0, "bid": 9.99, "ask": 10.01,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]
    before = dict(engine.paper.cash_by_currency)
    assert engine.paused is False
    assert engine.cycle() == []
    assert engine.last_scan_at is not None
    assert engine.last_signals
    assert engine.paper.cash_by_currency == before
    assert engine.paper.positions == {}
    db.close()


def test_full_market_scan_monitors_all_pairs_and_can_analyze_non_eur_markets(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.watchlist = ()
    engine.s.trade_quote_currency = "EUR"
    engine.s.max_candle_pairs = 1

    class FullMarketExchange:
        def discover_pairs(self):
            return [
                {"symbol": "XBTEUR", "base": "XBT", "quote": "EUR", "status": "online"},
                {"symbol": "ETHEUR", "base": "ETH", "quote": "EUR", "status": "online"},
                {"symbol": "ETHUSD", "base": "ETH", "quote": "USD", "status": "online"},
                {"symbol": "USDZEUR", "base": "USD", "quote": "EUR", "status": "online"},
            ]

        def tickers(self):
            return [
                {"symbol": "XBTEUR", "quote": "EUR", "bid1Price": 99.95, "ask1Price": 100, "lastPrice": 99.98, "turnover24h": 50000},
                {"symbol": "ETHEUR", "quote": "EUR", "bid1Price": 49.99, "ask1Price": 50, "lastPrice": 49.995, "turnover24h": 20000},
                {"symbol": "ETHUSD", "quote": "USD", "bid1Price": 2999, "ask1Price": 3001, "lastPrice": 3000, "turnover24h": 500000},
                {"symbol": "USDZEUR", "quote": "EUR", "bid1Price": 0.8999, "ask1Price": 0.9001, "lastPrice": 0.9, "turnover24h": 100000},
            ]

    engine.exchange = FullMarketExchange()
    analyzed = []

    def fake_scan_one(symbol, tickers=None, **kwargs):
        analyzed.append(symbol)
        return {"symbol": symbol, "signal": "HOLD", "score": 1, "price": 99, "spread_bps": 10, "turnover": 50000, "ohlc_analyzed": True, "reasons": [f"Analyzed {symbol}"]}

    engine.scan_one = fake_scan_one
    rows = engine.scan()

    assert engine.market_ticker_count == 4
    assert len(rows) == 4
    assert analyzed == ["ETHUSD"]
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["ETHUSD"]["signal"] == "HOLD"
    assert by_symbol["ETHUSD"]["ohlc_analyzed"] is True
    assert "Analyzed ETHUSD" in by_symbol["ETHUSD"]["reasons"][0]
    assert "deferred" in by_symbol["ETHEUR"]["reasons"][0]
    db.close()


def test_rotating_scan_covers_multiple_funded_quote_currencies(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.watchlist = ()
    engine.paper.cash_by_currency["USD"] = 20
    engine.s.max_candle_pairs = 1
    class MultiQuoteExchange:
        def discover_pairs(self):
            return [
                {"symbol": "XBTEUR", "base": "XBT", "quote": "EUR"},
                {"symbol": "ETHUSD", "base": "ETH", "quote": "USD"},
                {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
            ]
        def tickers(self):
            return [
                {"symbol": "XBTEUR", "bid1Price": 99.95, "ask1Price": 100, "lastPrice": 99.98, "turnover24h": 50000},
                {"symbol": "ETHUSD", "bid1Price": 2999.5, "ask1Price": 3000, "lastPrice": 2999.8, "turnover24h": 40000},
                {"symbol": "USDZEUR", "bid1Price": 0.8999, "ask1Price": 0.9001, "lastPrice": 0.9, "turnover24h": 10000},
            ]
    engine.exchange = MultiQuoteExchange()
    seen = []
    def fake_scan_one(symbol, tickers=None, **kwargs):
        seen.append(symbol)
        return {"symbol": symbol, "signal": "HOLD", "score": 1, "price": 1, "spread_bps": 10, "turnover": 50000, "reasons": []}
    engine.scan_one = fake_scan_one
    first = engine.scan()
    second = engine.scan()
    assert seen == ["XBTEUR", "ETHUSD"]
    assert {row["quote"] for row in first + second} == {"EUR", "USD"}
    db.close()

 
 
def test_non_eur_trade_spends_existing_quote_currency_without_converting_eur(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.max_positions = 2
    engine.s.max_trade = 5
    engine.paper.cash_by_currency["USD"] = 20
    engine.market_pairs = [
        {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
        {"symbol": "ETHUSD", "base": "ETH", "quote": "USD"},
    ]
    engine.exchange = DummyExchange()
    engine.last_prices = {"USDZEUR": 0.9, "ETHUSD": 3000}
    engine.scan = lambda **kwargs: [
        {"symbol": "USDZEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
         "price": 0.9, "bid": 0.899, "ask": 0.901, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
        {"symbol": "ETHUSD", "quote": "USD", "signal": "BUY", "score": 10,
         "price": 3000, "bid": 2999, "ask": 3001, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
    ]
    engine.start()
    actions = engine.cycle()
    assert len(actions) == 1
    assert "ETHUSD BUY" in actions[0]
    assert engine.paper.balance("EUR") == 20
    assert engine.paper.balance("USD") < 20
    assert engine.paper.positions["ETHUSD"]["quote_currency"] == "USD"
    db.close()


def test_non_eur_signal_can_convert_eur_to_fund_quote_wallet(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.max_trade = 5
    engine.market_pairs = [
        {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
        {"symbol": "ETHUSD", "base": "ETH", "quote": "USD"},
    ]
    engine.last_prices = {"USDZEUR": 0.9, "ETHUSD": 3000}
    engine.scan = lambda **kwargs: [
        {"symbol": "USDZEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
         "price": 0.9, "bid": 0.899, "ask": 0.901, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
        {"symbol": "ETHUSD", "quote": "USD", "signal": "BUY", "score": 10,
         "price": 3000, "bid": 2999, "ask": 3001, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
    ]
    engine.start()
    actions = engine.cycle()
    assert len(actions) == 1
    assert "ETHUSD BUY" in actions[0]
    assert engine.paper.balance("EUR") < 20
    assert engine.paper.balance("USD") >= 0
    assert engine.paper.positions["ETHUSD"]["quote_currency"] == "USD"
    row = db.execute("SELECT COUNT(*) AS n FROM audit WHERE event='PAPER_FX_CONVERSION'").fetchone()
    assert row["n"] == 1
    db.close()


def test_wallet_seeds_usd_and_usdt_from_starting_eur_once(tmp_path):
    engine, db = make_engine(tmp_path)
    buy_rates = {"EUR": 1.0, "USD": 1 / 0.9, "USDT": 1 / 0.9}
    engine._seed_multicurrency_wallet(buy_rates)
    assert engine.paper.balance("EUR") == 10
    assert engine.paper.balance("USD") == pytest.approx(5 / 0.9 * (1 - engine.paper.fee_rate))
    assert engine.paper.balance("USDT") == pytest.approx(5 / 0.9 * (1 - engine.paper.fee_rate))
    before = dict(engine.paper.cash_by_currency)
    engine._seed_multicurrency_wallet(buy_rates)
    assert engine.paper.cash_by_currency == before
    rows = db.execute("SELECT COUNT(*) AS n FROM audit WHERE event='PAPER_WALLET_SEED'").fetchone()
    assert rows["n"] == 2
    db.close()


def test_fx_rates_convert_quote_currencies_to_eur(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.market_pairs = [
        {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
        {"symbol": "ETHEUR", "base": "ETH", "quote": "EUR"},
    ]
    rates = engine._fx_rates({"USDZEUR": 0.9, "ETHEUR": 2700})
    assert rates["USD"] == 0.9
    assert rates["ETH"] == 2700
    db.close()

def test_manual_market_scan_does_not_rebalance_paper_wallet(tmp_path):
    engine, db = make_engine(tmp_path)

    class FxExchange:
        def discover_pairs(self):
            return [
                {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
                {"symbol": "USDTZEUR", "base": "USDT", "quote": "EUR"},
            ]

        def tickers(self):
            return [
                {"symbol": "USDZEUR", "bid1Price": 0.8999, "ask1Price": 0.9001, "lastPrice": 0.9, "turnover24h": 50000},
                {"symbol": "USDTZEUR", "bid1Price": 0.89, "ask1Price": 0.90, "lastPrice": 0.895, "turnover24h": 50000},
            ]

    engine.exchange = FxExchange()
    engine.scan_one = lambda symbol, tickers=None, **kwargs: {
        "symbol": symbol, "signal": "HOLD", "score": 0, "price": 1,
        "spread_bps": 10, "turnover": 50000, "reasons": [],
    }
    before = dict(engine.paper.cash_by_currency)

    engine.scan()

    assert engine.paper.cash_by_currency == before
    assert engine.paper.balance("USD") == 0
    assert engine.paper.balance("USDT") == 0
    db.close()


def test_engine_trailing_stop_closes_position_after_retracement(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.trailing_stop_pct = 1.5
    engine.paper.buy('XBTEUR', 5, 10, quote_to_eur_rate=1.0)
    engine.paper.update_peak('XBTEUR', 10.5)
    engine.scan = lambda **kwargs: [{'symbol': 'XBTEUR', 'quote': 'EUR', 'signal': 'HOLD', 'score': 0, 'price': 10.3, 'bid': 10.29, 'ask': 10.31, 'spread_bps': 10, 'turnover': 50000, 'turnover_eur': 50000, 'reasons': []}]
    engine.start()
    actions = engine.cycle()
    assert any('XBTEUR SELL TRAILING_STOP' in action for action in actions)
    assert 'XBTEUR' not in engine.paper.positions
    row = db.execute("SELECT event FROM audit WHERE event='TRAILING_STOP'").fetchone()
    assert row is not None
    db.close()


def test_buy_candidates_are_ranked_by_score(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.max_positions = 1
    engine.s.max_trade = 5
    engine.scan = lambda **kwargs: [
        {'symbol': 'LOWEUR', 'quote': 'EUR', 'signal': 'BUY', 'score': 2, 'price': 10, 'bid': 9.99, 'ask': 10.01, 'spread_bps': 10, 'turnover': 50000, 'turnover_eur': 50000, 'reasons': []},
        {'symbol': 'HIGHEUR', 'quote': 'EUR', 'signal': 'BUY', 'score': 5, 'price': 10, 'bid': 9.99, 'ask': 10.01, 'spread_bps': 10, 'turnover': 50000, 'turnover_eur': 50000, 'reasons': []},
    ]
    engine.start()
    actions = engine.cycle()
    assert len(actions) == 1
    assert 'HIGHEUR BUY' in actions[0]
    db.close()


def test_scan_analyzes_markets_with_fx_route_without_quote_cash(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.max_candle_pairs = 5
    class FxRoutedExchange:
        def discover_pairs(self):
            return [
                {'symbol': 'USDZEUR', 'base': 'USD', 'quote': 'EUR'},
                {'symbol': 'ETHUSD', 'base': 'ETH', 'quote': 'USD'},
            ]
        def tickers(self):
            return [
                {'symbol': 'USDZEUR', 'bid1Price': 0.8999, 'ask1Price': 0.9001, 'lastPrice': 0.9, 'turnover24h': 50000},
                {'symbol': 'ETHUSD', 'bid1Price': 2999, 'ask1Price': 3001, 'lastPrice': 3000, 'turnover24h': 50000},
            ]
    engine.exchange = FxRoutedExchange()
    analyzed = []
    def fake_scan_one(symbol, tickers=None, **kwargs):
        analyzed.append(symbol)
        return {'symbol': symbol, 'signal': 'HOLD', 'score': 1, 'price': 1, 'spread_bps': 10, 'turnover': 50000, 'reasons': []}
    engine.scan_one = fake_scan_one
    engine.scan()
    assert 'ETHUSD' in analyzed
    assert engine.paper.balance('USD') == 0
    db.close()



def test_paper_buy_crosses_spread_at_ask(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "BUY", "score": 5,
        "price": 100.0, "bid": 99.0, "ask": 101.0,
        "spread_bps": 20, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]
    engine.start()
    actions = engine.cycle()
    assert len(actions) == 1
    assert engine.paper.positions["XBTEUR"]["avg_price"] == pytest.approx(101.0505)
    trade = db.execute("SELECT price FROM trades WHERE side='Buy'").fetchone()
    assert trade["price"] == pytest.approx(101.0505)
    db.close()


def test_paper_stop_uses_executable_bid_not_last_trade(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 98.5, "bid": 97.9, "ask": 99.1,
        "spread_bps": 20, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]
    engine.start()
    actions = engine.cycle()
    assert any("XBTEUR SELL STOP_LOSS" in action for action in actions)
    trade = db.execute("SELECT price FROM trades WHERE side='Sell'").fetchone()
    assert trade["price"] == pytest.approx(97.9 * 0.9995)
    assert "XBTEUR" not in engine.paper.positions
    db.close()



def test_saved_fx_snapshot_values_non_eur_wallet_after_restart(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 10.0
    engine.paper.buy("ETHUSD", 5.0, 10.0, currency="USD")
    set_state(db, "paper_fx_rates", {"EUR": 1.0, "USD": 0.9})

    rates = engine._fx_rates({})

    assert rates["USD"] == pytest.approx(0.9)
    assert engine.paper.equity({"ETHUSD": 10.0}, rates) == pytest.approx(
        20.0 + engine.paper.balance("USD") * 0.9
        + engine.paper.positions["ETHUSD"]["qty"] * 10.0 * 0.9
    )
    db.close()


def test_position_size_shrinks_when_atr_volatility_increases(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.max_trade = 5
    engine.s.risk_per_trade_pct = 0.5
    engine.scan = lambda **kwargs: [{
        "symbol": "VOLATILEEUR", "quote": "EUR", "signal": "BUY", "score": 5,
        "price": 10.0, "bid": 9.99, "ask": 10.01, "spread_bps": 10,
        "turnover": 50000, "turnover_eur": 50000, "atr_percent": 10.0, "reasons": [],
    }]
    engine.start()
    actions = engine.cycle()
    assert len(actions) == 1
    trade = db.execute("SELECT quote FROM trades WHERE side='Buy'").fetchone()
    # 0.5% of EUR20 risk budget / (1.5 * 10% ATR) = about EUR0.67.
    assert trade["quote"] == pytest.approx(20 * 0.005 / 0.15, rel=0.02)
    db.close()



def test_cycle_uses_market_snapshot_timestamp_not_scan_completion(tmp_path):
    from datetime import datetime, timezone

    engine, db = make_engine(tmp_path)
    snapshot_time = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)

    def scan_with_snapshot_time(**kwargs):
        engine.market_snapshot_at = snapshot_time
        return [{
            "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
            "price": 10.0, "bid": 9.99, "ask": 10.01, "spread_bps": 10,
            "turnover": 50000, "turnover_eur": 50000,
            "ohlc_analyzed": True, "reasons": [],
        }]

    engine.scan = scan_with_snapshot_time
    engine.start()
    engine.cycle()

    assert engine.last_scan_at == snapshot_time
    db.close()


def test_successful_cycle_persists_paper_snapshot(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 10.0, "bid": 9.99, "ask": 10.01, "spread_bps": 10,
        "turnover": 50000, "turnover_eur": 50000, "ohlc_analyzed": True, "reasons": [],
    }]
    engine.start()

    engine.cycle()

    row = db.execute(
        "SELECT equity_eur, analyzed_markets, buy_signals FROM paper_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["equity_eur"] > 0
    assert row["analyzed_markets"] == 1
    assert row["buy_signals"] == 0
    db.close()



def test_paused_cycle_refreshes_snapshot_using_fresh_market_valuation(tmp_path):
    from datetime import datetime, timezone
    from src.trader.db import get_latest_paper_snapshot

    engine, db = make_engine(tmp_path)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 12.0, "bid": 11.99, "ask": 12.01,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "ohlc_analyzed": False, "reasons": [],
    }]
    engine.market_pairs = [{"symbol": "XBTEUR", "base": "XBT", "quote": "EUR"}]
    engine.stop("test paused snapshot refresh")

    engine.cycle()

    snapshot = get_latest_paper_snapshot(db)
    assert snapshot is not None
    assert snapshot["equity_eur"] == pytest.approx(20.0)
    assert snapshot["open_positions"] == 0
    assert datetime.now(timezone.utc).replace(tzinfo=None) - datetime.strptime(
        snapshot["ts"], "%Y-%m-%d %H:%M:%S"
    ) < __import__("datetime").timedelta(minutes=1)
    assert engine.paused is True
    db.close()


def test_paused_cycle_does_not_save_snapshot_with_missing_fresh_fx(tmp_path):
    from src.trader.db import get_latest_paper_snapshot

    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 5.0
    engine.paper.cash_by_currency["EUR"] -= 5.0
    engine.paper._sync_cash()
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 12.0, "bid": 11.99, "ask": 12.01,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "ohlc_analyzed": False, "reasons": [],
    }]
    engine.market_pairs = [{"symbol": "XBTEUR", "base": "XBT", "quote": "EUR"}]
    engine.stop("test paused snapshot without FX")

    engine.cycle()

    assert get_latest_paper_snapshot(db) is None
    assert engine.paused is True
    row = db.execute("SELECT details FROM audit WHERE event='PAPER_SNAPSHOT_FAILED'").fetchone()
    assert row is not None
    assert "USD" in row["details"]
    db.close()


def test_cached_fx_rate_cannot_authorize_new_conversion(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.market_pairs = [{"symbol": "USDZEUR", "base": "USD", "quote": "EUR"}]
    set_state(db, "paper_fx_rates", {"EUR": 1.0, "USD": 0.9})

    assert engine._fx_rates({})["USD"] == pytest.approx(0.9)
    assert "USD" not in engine._fresh_fx_rates({})
    db.close()


def test_missing_open_position_price_fails_closed(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 10)
    engine.scan = lambda **kwargs: []
    engine.start()
    actions = engine.cycle()
    assert engine.paused is True
    assert any("quote unavailable" in action for action in actions)
    assert "XBTEUR" in engine.paper.positions
    db.close()


def test_missing_fresh_fx_route_blocks_new_trades(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 5
    engine.market_pairs = []
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "BUY", "score": 5,
        "price": 10.0, "bid": 9.99, "ask": 10.01,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]
    engine.start()
    actions = engine.cycle()
    assert engine.paused is True
    assert any("FX_ROUTE_MISSING" in action for action in actions)
    assert engine.paper.positions == {}
    db.close()


def test_daily_baseline_is_captured_before_first_cycle_exit_after_midnight(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 10)
    set_state(db, "daily", {"date": "2000-01-01", "start_equity": 20.0, "realized": 0.0})
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 9.7, "bid": 9.7, "ask": 9.8,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]
    engine.start()
    actions = engine.cycle()
    daily = get_state(db, "daily")
    assert any("XBTEUR SELL STOP_LOSS" in action for action in actions)
    assert daily["start_equity"] == pytest.approx(14.987 + 0.5 * 9.7 * 0.9995 * (1 - engine.paper.fee_rate))
    # The new UTC-day baseline is net liquidation value, so exiting at the
    # same executable bid does not manufacture a same-day loss.
    assert engine.daily_pnl(engine.last_prices) == pytest.approx(0.0)
    db.close()


def test_wallet_seed_never_uses_cached_stale_fx_rates(tmp_path):
    engine, db = make_engine(tmp_path)
    set_state(db, "paper_fx_rates", {"EUR": 1.0, "USD": 0.9, "USDT": 0.9})

    class StaleFxOnlyExchange:
        def discover_pairs(self):
            return [
                {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
                {"symbol": "USDTZEUR", "base": "USDT", "quote": "EUR"},
            ]
        def tickers(self):
            return []

    engine.exchange = StaleFxOnlyExchange()
    engine.scan(seed_wallet=True)

    assert engine.paper.balance("EUR") == pytest.approx(20)
    assert engine.paper.balance("USD") == 0
    assert engine.paper.balance("USDT") == 0
    db.close()


def test_stale_cached_fx_cannot_create_buy_candidate_for_unpriced_quote_currency(tmp_path):
    engine, db = make_engine(tmp_path)
    set_state(db, "paper_fx_rates", {"EUR": 1.0, "USD": 0.9})

    class CachedFxExchange:
        def discover_pairs(self):
            return [{"symbol": "BTCUSD", "base": "BTC", "quote": "USD"}]
        def tickers(self):
            return [{
                "symbol": "BTCUSD", "bid1Price": 999.0, "ask1Price": 1001.0,
                "lastPrice": 1000.0, "turnover24h": 1000000.0,
            }]

    engine.exchange = CachedFxExchange()
    rows = engine.scan()
    assert rows[0]["signal"] == "HOLD"
    assert rows[0]["ohlc_analyzed"] is False
    assert "no fresh EUR conversion route" in rows[0]["reasons"][0]
    db.close()


def test_invalid_bid_ask_for_open_position_fails_closed(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 100.0, "bid": 0.0, "ask": 100.1,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": [],
    }]
    engine.start()

    actions = engine.cycle()

    assert engine.paused is True
    assert any("quote unavailable" in action for action in actions)
    assert "XBTEUR" in engine.paper.positions
    db.close()


def test_strategy_sell_signal_closes_position_at_bid(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0, quote_to_eur_rate=1.0)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "SELL", "score": 0,
        "price": 102.0, "bid": 102.0, "ask": 102.1,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": ["strategy exit"],
    }]
    engine.start()

    actions = engine.cycle()

    assert any("XBTEUR SELL SIGNAL_SELL" in action for action in actions)
    assert "XBTEUR" not in engine.paper.positions
    trade = db.execute("SELECT side,price FROM trades WHERE side='Sell' ORDER BY id DESC LIMIT 1").fetchone()
    assert trade["side"] == "Sell"
    assert trade["price"] == pytest.approx(102.0 * 0.9995)
    db.close()



def test_strategy_sell_is_deferred_until_net_profit_after_all_costs(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0, quote_to_eur_rate=1.0)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "SELL", "score": 0,
        "price": 100.1, "bid": 100.1, "ask": 100.2,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": ["strategy exit"],
    }]
    engine.start()

    actions = engine.cycle()

    assert "XBTEUR" in engine.paper.positions
    assert any("sale deferred" in action for action in actions)
    assert db.execute("SELECT COUNT(*) FROM trades WHERE side='Sell'").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM audit WHERE event='PAPER_SELL_DEFERRED_NON_PROFIT'"
    ).fetchone()[0] == 1
    db.close()


def test_open_position_equity_uses_bid_not_last_trade(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 110.0, "bid": 99.0, "ask": 110.0,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": [],
    }]
    engine.start()

    engine.cycle()

    assert "XBTEUR" in engine.paper.positions
    snapshot = db.execute(
        "SELECT equity_eur FROM paper_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert snapshot is not None
    assert snapshot["equity_eur"] == pytest.approx(14.987 + 0.05 * 99.0 * 0.9995 * (1 - engine.paper.fee_rate))
    db.close()


def test_fresh_fx_does_not_route_through_an_unstable_crypto_bridge(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.market_pairs = [
        {"symbol": "XBTEUR", "base": "XBT", "quote": "EUR"},
        {"symbol": "XBTUSD", "base": "XBT", "quote": "USD"},
    ]
    rates = engine._fresh_fx_rates({"XBTEUR": 60000, "XBTUSD": 65000})
    assert rates["XBT"] == pytest.approx(60000)
    assert "USD" not in rates
    db.close()


def test_fresh_fx_rejects_wide_spread_direct_eur_market(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.market_pairs = [{"symbol": "USDZEUR", "base": "USD", "quote": "EUR"}]
    engine.last_ticker_data = {
        "USDZEUR": {"bid1Price": 0.89, "ask1Price": 0.90, "turnover24h": 100000}
    }
    rates = engine._fresh_fx_rates({"USDZEUR": 0.895})
    assert "USD" not in rates
    db.close()


def test_fresh_fx_rejects_illiquid_direct_eur_market(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.market_pairs = [{"symbol": "USDZEUR", "base": "USD", "quote": "EUR"}]
    engine.last_ticker_data = {
        "USDZEUR": {"bid1Price": 0.8999, "ask1Price": 0.9001, "turnover24h": 500}
    }
    rates = engine._fresh_fx_rates({"USDZEUR": 0.9})
    assert "USD" not in rates
    db.close()


def test_fresh_fx_valuation_and_purchase_rates_use_executable_sides(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.market_pairs = [{"symbol": "USDZEUR", "base": "USD", "quote": "EUR"}]
    engine.last_prices = {"USDZEUR": 0.9}
    engine.last_ticker_data = {
        "USDZEUR": {
            "bid1Price": 0.899, "ask1Price": 0.901,
            "lastPrice": 0.9, "turnover24h": 50000,
        }
    }

    valuation_rates = engine._fresh_fx_rates(engine.last_prices)
    buy_rates = engine._fresh_fx_buy_rates(engine.last_prices)

    assert valuation_rates["USD"] == pytest.approx(0.899)
    assert buy_rates["USD"] == pytest.approx(1 / 0.901)
    assert buy_rates["USD"] < 1 / valuation_rates["USD"]
    db.close()


def test_daily_loss_kill_switch_liquidates_open_positions_at_bid(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0)
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    set_state(db, "daily", {"date": today, "start_equity": 30.0, "realized": 0.0})
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 99.0, "bid": 99.0, "ask": 99.1,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": [],
    }]
    engine.start()

    actions = engine.cycle()

    assert engine.paused is True
    assert "XBTEUR" not in engine.paper.positions
    assert any("XBTEUR SELL DAILY_LOSS_LIMIT" in action for action in actions)
    assert actions[-1] == "DAILY LOSS LIMIT — paused"
    trade = db.execute("SELECT side,price FROM trades WHERE side='Sell' ORDER BY id DESC LIMIT 1").fetchone()
    assert trade["side"] == "Sell"
    assert trade["price"] == pytest.approx(99.0 * 0.9995)
    row = db.execute("SELECT COUNT(*) AS n FROM audit WHERE event='DAILY_LOSS_LIQUIDATION'").fetchone()
    assert row["n"] == 1
    db.close()


def test_non_eur_cross_conversion_charges_two_fee_legs(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["EUR"] = 0.0
    engine.paper.cash_by_currency["USD"] = 20.0
    engine.paper._sync_cash()
    engine.market_pairs = [
        {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
        {"symbol": "USDTZEUR", "base": "USDT", "quote": "EUR"},
        {"symbol": "ETHUSDT", "base": "ETH", "quote": "USDT"},
    ]
    engine.last_prices = {"USDZEUR": 0.9, "USDTZEUR": 0.9, "ETHUSDT": 3000.0}
    engine.scan = lambda **kwargs: [
        {"symbol": "USDZEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
         "price": 0.9, "bid": 0.899, "ask": 0.901, "spread_bps": 10,
         "turnover": 50000, "turnover_eur": 45000, "reasons": []},
        {"symbol": "USDTZEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
         "price": 0.9, "bid": 0.899, "ask": 0.901, "spread_bps": 10,
         "turnover": 50000, "turnover_eur": 45000, "reasons": []},
        {"symbol": "ETHUSDT", "quote": "USDT", "signal": "BUY", "score": 5,
         "price": 3000, "bid": 2999, "ask": 3001, "spread_bps": 10,
         "turnover": 50000, "turnover_eur": 45000, "reasons": []},
    ]
    engine.start()

    actions = engine.cycle()

    assert any("ETHUSDT BUY" in action for action in actions)
    row = db.execute(
        "SELECT details FROM audit WHERE event='PAPER_FX_CONVERSION' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert "legs=2" in row["details"]
    assert "fees=" in row["details"]
    assert engine.paper.balance("EUR") >= 0
    assert engine.paper.balance("USD") >= 0
    db.close()



def test_pure_fx_rate_builder_does_not_require_mutating_engine_state():
    from src.trader.engine import fresh_fx_rates_from_market

    settings = SimpleNamespace(max_spread_bps=25, min_turnover=10000)
    pairs = [
        {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
        {"symbol": "ZEURUSD", "base": "EUR", "quote": "USD"},
        {"symbol": "XBTUSD", "base": "XBT", "quote": "USD"},
    ]
    tickers = {
        "USDZEUR": {"bid1Price": 0.915, "ask1Price": 0.916, "turnover24h": 50000},
        "ZEURUSD": {"bid1Price": 1.090, "ask1Price": 1.091, "turnover24h": 60000},
        "XBTUSD": {"bid1Price": 60000, "ask1Price": 60010, "turnover24h": 1_000_000},
    }
    prices = {"USDZEUR": 0.9155, "ZEURUSD": 1.0905, "XBTUSD": 60005}

    rates = fresh_fx_rates_from_market(settings, pairs, tickers, prices)

    assert rates["EUR"] == 1.0
    assert rates["USD"] == pytest.approx(1 / 1.091)
    assert "XBT" not in rates


def test_scan_logs_stage_timing(caplog, tmp_path):
    engine, db = make_engine(tmp_path)

    with caplog.at_level("INFO", logger="src.trader.engine"):
        engine.scan()

    assert any("Market scan timing:" in record.message for record in caplog.records)
    timing = next(record.message for record in caplog.records if "Market scan timing:" in record.message)
    assert "ticker=" in timing
    assert "OHLC=" in timing
    assert "total=" in timing
    saved = get_state(db, "last_scan_metrics", {})
    assert saved == engine.last_scan_metrics
    assert saved["duration_seconds"] >= 0
    restored = Engine(engine.s, db, DummyExchange(), PaperBroker(20, .0026, db=db))
    assert restored.last_scan_metrics == saved
    db.close()


def test_priority_market_selection_keeps_ton_and_open_positions_fresh():
    from src.trader.engine import select_ohlc_markets

    eligible = [
        (100000 - index, f"COIN{index}EUR", "EUR")
        for index in range(30)
    ] + [(6000, "OPENPOSITIONEUR", "EUR"), (5000, "TONEUR", "EUR"), (4000, "TONUSD", "USD")]
    selected, next_cursor, priority_count = select_ohlc_markets(
        eligible, 5, 0, ["OPENPOSITIONEUR", "TONEUR", "TONUSD"]
    )
    assert ("OPENPOSITIONEUR", "EUR") in selected
    assert ("TONEUR", "EUR") in selected
    assert ("TONUSD", "USD") in selected
    assert len(selected) == 5
    assert next_cursor == 5
    assert priority_count == 3


def test_priority_market_selection_rotates_remaining_markets_without_duplicates():
    from src.trader.engine import select_ohlc_markets

    eligible = [(1000 - index, f"COIN{index}EUR", "EUR") for index in range(12)]
    first, cursor, _ = select_ohlc_markets(eligible, 4, 0, ["COIN0EUR"])
    second, next_cursor, _ = select_ohlc_markets(eligible, 4, cursor, ["COIN0EUR"])
    assert len(first) == len(second) == 4
    assert ("COIN0EUR", "EUR") in first
    assert ("COIN0EUR", "EUR") in second
    assert len((first - {("COIN0EUR", "EUR")}) | (second - {("COIN0EUR", "EUR")})) >= 5
    assert next_cursor == 8

def test_paper_execution_prices_include_configured_slippage(tmp_path):
    engine, db = make_engine(tmp_path)
    assert engine._paper_buy_price(100.0) == pytest.approx(100.05)
    assert engine._paper_sell_price(100.0) == pytest.approx(99.95)
    db.close()


def test_paper_liquidation_equity_reserves_exit_fee_and_slippage(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0)
    value = engine._equity_after_liquidation({"XBTEUR": 100.0}, {"EUR": 1.0})
    expected = engine.paper.balance("EUR") + 0.05 * 100.0 * (1 - 0.0005) * (1 - engine.paper.fee_rate)
    assert value == pytest.approx(expected)
    db.close()


def test_paper_sell_fill_applies_slippage_to_executable_bid(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0, quote_to_eur_rate=1.0)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "SELL", "score": 0,
        "price": 101.0, "bid": 101.0, "ask": 101.1,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]
    engine.start()
    actions = engine.cycle()
    row = db.execute("SELECT price FROM trades WHERE side='Sell' ORDER BY id DESC LIMIT 1").fetchone()
    assert any("SELL SIGNAL_SELL" in action for action in actions)
    assert row["price"] == pytest.approx(101.0 * 0.9995)
    db.close()


def test_liquidation_equity_includes_cash_and_open_positions_after_exit_costs(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency = {"EUR": 5.0}
    engine.paper.positions = {
        "XBTEUR": {
            "qty": 0.1, "avg_price": 100.0, "quote_currency": "EUR",
            "cost_basis_eur": 10.0, "entry_fees_eur": 0.026,
        }
    }

    equity = engine._equity_after_liquidation(
        {"XBTEUR": 110.0}, {"EUR": 1.0},
    )
    expected_position_value = 0.1 * 110.0 * (1 - 5 / 10000) * (1 - engine.paper.fee_rate)

    assert equity == pytest.approx(5.0 + expected_position_value)
    assert equity > engine.paper.balance("EUR")
    db.close()


def test_adaptive_ohlc_budget_leaves_headroom_and_respects_configured_cap():
    from src.trader.engine import adaptive_ohlc_budget

    assert adaptive_ohlc_budget(20, 60, 3.0) == 11
    assert adaptive_ohlc_budget(20, 60, 2.6) == 12
    assert adaptive_ohlc_budget(20, 60, 1.0) == 20
    assert adaptive_ohlc_budget(5, 60, 30.0) == 1
    assert adaptive_ohlc_budget(20, 600, 3.0) == 20


@pytest.mark.parametrize("args", [
    (0, 60, 1.0),
    (10, 0, 1.0),
    (10, 60, 0),
    (10, 60, float("nan")),
    (10, 60, 1.0, 1.0),
])
def test_adaptive_ohlc_budget_rejects_invalid_inputs(args):
    from src.trader.engine import adaptive_ohlc_budget

    with pytest.raises(ValueError):
        adaptive_ohlc_budget(*args)


def test_engine_restarts_paused_with_paper_wallet_restored(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 10)
    expected_cash = dict(engine.paper.cash_by_currency)
    expected_positions = {key: dict(value) for key, value in engine.paper.positions.items()}
    engine.stop("test explicitly persists paused state")
    db.close()

    db2 = connect(tmp_path / "engine.sqlite3")
    restored_paper = PaperBroker(20, .0026, db=db2)
    restored_engine = Engine(engine.s, db2, DummyExchange(), restored_paper)

    assert restored_engine.paused is True
    assert restored_paper.cash_by_currency == expected_cash
    assert restored_paper.positions == expected_positions
    db2.close()


def test_engine_paper_fill_commits_wallet_and_trade_together(tmp_path):
    engine, db = make_engine(tmp_path)
    before = engine.paper.balance("EUR")

    fill, fee_eur, pnl_eur = engine._execute_paper_fill(
        "Buy", "XBTEUR", 5.0, 10.0, {}, currency="EUR",
    )

    from src.trader.db import get_state
    saved = get_state(db, "paper_broker")
    row = db.execute(
        "SELECT side,qty,status,fee_eur FROM trades WHERE symbol='XBTEUR'"
    ).fetchone()
    assert fill["status"] == "FILLED"
    assert engine.paper.balance("EUR") < before
    assert saved["cash_by_currency"]["EUR"] == pytest.approx(engine.paper.balance("EUR"))
    assert saved["positions"]["XBTEUR"]["qty"] == pytest.approx(fill["qty"])
    assert row["side"] == "Buy"
    assert row["status"] == "FILLED"
    assert row["fee_eur"] == pytest.approx(fee_eur)
    assert pnl_eur is None
    db.close()


def test_engine_paper_fill_rolls_back_memory_when_database_commit_fails(tmp_path):
    import sqlite3
    engine, db = make_engine(tmp_path)
    before_cash = dict(engine.paper.cash_by_currency)
    before_positions = dict(engine.paper.positions)
    db.execute(
        "CREATE TRIGGER reject_engine_paper_trade BEFORE INSERT ON trades "
        "BEGIN SELECT RAISE(ABORT, 'forced persistence failure'); END"
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        engine._execute_paper_fill(
            "Buy", "XBTEUR", 5.0, 10.0, {}, currency="EUR",
        )

    assert engine.paper.cash_by_currency == before_cash
    assert engine.paper.positions == before_positions
    assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    from src.trader.db import get_state
    saved = get_state(db, "paper_broker")
    assert saved["cash_by_currency"] == before_cash
    assert saved["positions"] == before_positions
    db.close()

def test_shadow_research_uses_configured_paper_slippage(tmp_path, monkeypatch):
    import src.trader.engine as engine_module

    engine, db = make_engine(tmp_path)
    engine.s.paper_slippage_bps = 17.0
    engine.s.fee_rate = 0.0026
    set_state(db, "ml_candidate_XBTEUR", {
        "model": {"version": 1},
        "model_version": "test",
        "probability_threshold": 0.58,
        "horizon_candles": 3,
    })
    captured = {}
    monkeypatch.setattr(engine_module, "analyze", lambda candles: {
        "signal": "HOLD", "score": 0, "market_regime": "RANGE", "reasons": [],
    })
    monkeypatch.setattr(
        engine_module, "settle_shadow_predictions",
        lambda *args, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(engine_module, "extract_features", lambda candles, end: {"feature": 1})
    monkeypatch.setattr(engine_module, "predict_probability", lambda model, features: 0.5)
    monkeypatch.setattr(engine_module, "record_shadow_prediction", lambda *args, **kwargs: None)

    candles = [[float(i), 0.0, 101.0, 99.0, 100.0, 1.0] for i in range(61)]
    monkeypatch.setattr(engine.exchange, "candles", lambda *args, **kwargs: candles)
    tickers = [{
        "symbol": "XBTEUR", "bid1Price": "99", "ask1Price": "101",
        "lastPrice": "100", "turnover24h": "1000000",
    }]
    assert get_state(db, "ml_candidate_XBTEUR")["model"]
    result = engine.scan_one("XBTEUR", tickers=tickers, fx_rates={"EUR": 1.0}, quote_currency="EUR")

    assert result["ohlc_analyzed"] is True
    shadow_error = db.execute(
        "SELECT details FROM audit WHERE event='ML_SHADOW_ERROR' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert captured.get("slippage_bps") == pytest.approx(17.0), (
        shadow_error["details"] if shadow_error else "shadow settlement was not called"
    )
    db.close()


def test_paper_slippage_rate_converts_and_rejects_out_of_range_values(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.paper_slippage_bps = 17.0
    assert engine._slippage_rate() == pytest.approx(0.0017)

    engine.s.paper_slippage_bps = 100.0
    assert engine._slippage_rate() == pytest.approx(0.01)

    for invalid in (-0.01, 100.01, float("nan"), float("inf")):
        engine.s.paper_slippage_bps = invalid
        with pytest.raises(ValueError):
            engine._slippage_rate()
    db.close()

def test_trailing_stop_remains_authoritative_when_exit_is_net_negative(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.s.trailing_stop_pct = 1.5
    engine.paper.buy("XBTEUR", 5, 100.0, quote_to_eur_rate=1.0)
    # A modest peak followed by a retracement below entry triggers the
    # trailing stop without crossing the separate hard stop-loss threshold.
    engine.paper.update_peak("XBTEUR", 100.5)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "HOLD", "score": 0,
        "price": 98.95, "bid": 98.9, "ask": 99.0,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": [],
    }]
    engine.start()

    actions = engine.cycle()

    assert any("XBTEUR SELL TRAILING_STOP" in action for action in actions)
    assert "XBTEUR" not in engine.paper.positions
    trade = db.execute(
        "SELECT price FROM trades WHERE side='Sell' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert trade is not None
    assert trade["price"] == pytest.approx(98.9 * 0.9995)
    assert db.execute(
        "SELECT COUNT(*) FROM audit WHERE event='PAPER_SELL_DEFERRED_NON_PROFIT'"
    ).fetchone()[0] == 0
    db.close()


def test_scan_candle_cache_reuses_closed_candles_until_timeframe_boundary(tmp_path, monkeypatch):
    engine, db = make_engine(tmp_path)

    class CandleExchange:
        def __init__(self):
            self.calls = []
        def candles(self, symbol, interval="60", limit=120):
            self.calls.append((symbol, interval, limit))
            return [[1, 10, 11, 9, 10.5, 100]]

    exchange = CandleExchange()
    engine.exchange = exchange
    monkeypatch.setattr("src.trader.engine.time.time", lambda: 3600 * 100 + 3599)

    first = engine._scan_candles("XBTEUR", interval="60", limit=120)
    second = engine._scan_candles("XBTEUR", interval="60", limit=120)
    assert first == second
    assert len(exchange.calls) == 1

    first[0][4] = 999
    assert engine._scan_candles("XBTEUR", interval="60", limit=120)[0][4] == 10.5

    monkeypatch.setattr("src.trader.engine.time.time", lambda: 3600 * 101 + 9)
    engine._scan_candles("XBTEUR", interval="60", limit=120)
    assert len(exchange.calls) == 1

    monkeypatch.setattr("src.trader.engine.time.time", lambda: 3600 * 101 + 10)
    engine._scan_candles("XBTEUR", interval="60", limit=120)
    assert len(exchange.calls) == 2
    assert engine._scan_candle_cache_misses == 2
    assert engine._scan_candle_cache_hits == 3
    db.close()


def test_scan_candle_cache_does_not_cache_failed_exchange_requests(tmp_path, monkeypatch):
    engine, db = make_engine(tmp_path)

    class BrokenCandleExchange:
        calls = 0
        def candles(self, symbol, interval="60", limit=120):
            self.calls += 1
            raise TimeoutError("temporary OHLC outage")

    exchange = BrokenCandleExchange()
    engine.exchange = exchange
    monkeypatch.setattr("src.trader.engine.time.time", lambda: 3600 * 100 + 10)

    with pytest.raises(TimeoutError):
        engine._scan_candles("XBTEUR")
    with pytest.raises(TimeoutError):
        engine._scan_candles("XBTEUR")
    assert exchange.calls == 2
    db.close()



def test_paper_trade_report_formats_realized_eur_result_and_escapes_html():
    message = _format_paper_trade_action(
        "XBTEUR SELL STOP_LOSS | qty=0.12345678 | quote=EUR "
        "| entry=100 | exit=97.5 | change=-2.50% | fee_eur=0.0120 | net_pnl_eur=-0.3250"
    )
    assert "ЗАКРЫТА PAPER-ПОЗИЦИЯ" in message
    assert "Защитный Stop-Loss" in message
    assert "100 → 97.5" in message
    assert "Чистый реализованный результат" in message
    assert "-0.3250 EUR" in message
    assert "🔴" in message

    safe_message = _format_paper_trade_action(
        "XBT<EUR SELL STOP_LOSS | qty=1 | quote=EUR "
        "| entry=100 | exit=97 | change=-3% | fee_eur=0.1 | net_pnl_eur=-3"
    )
    assert "XBT&lt;EUR" in safe_message


def test_liquidation_equity_fails_closed_on_missing_position_mark(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 100.0)
    with pytest.raises(ValueError, match="Missing current PAPER mark price.*XBTEUR"):
        engine._equity_after_liquidation({}, {"EUR": 1.0})
    db.close()


def test_liquidation_equity_fails_closed_on_missing_cash_fx_rate(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 5.0
    with pytest.raises(ValueError, match="Missing or invalid PAPER cash FX valuation.*USD"):
        engine._equity_after_liquidation({}, {"EUR": 1.0})
    db.close()


def test_liquidation_equity_fails_closed_on_missing_position_fx_rate(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 0.0
    engine.paper.positions = {
        "ETHUSD": {"qty": 0.1, "avg_price": 100.0, "quote_currency": "USD"}
    }
    with pytest.raises(ValueError, match="Missing PAPER position FX valuation.*ETHUSD"):
        engine._equity_after_liquidation({"ETHUSD": 100.0}, {"EUR": 1.0})
    db.close()


def test_daily_pnl_does_not_treat_missing_open_position_marks_as_zero(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5, 10)
    with pytest.raises(ValueError, match="open positions have no current market marks"):
        engine.daily_pnl({})
    db.close()


def test_daily_risk_baseline_rejects_cached_only_fx_rate(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 5.0
    set_state(db, "paper_fx_rates", {"USD": 0.9})
    set_state(db, "daily", {"date": "2000-01-01", "start_equity": 20.0, "realized": 0.0})
    with pytest.raises(ValueError, match="Missing or invalid PAPER cash FX valuation.*USD"):
        engine._ensure_day({"SOME_MARKET": 1.0})
    db.close()

class OrderBookExchange:
    def __init__(self, book):
        self.book = book

    def order_book(self, symbol, depth=10):
        return self.book


def test_paper_buy_uses_visible_ask_depth_vwap_without_double_slippage(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.exchange = OrderBookExchange({
        "asks": [[101.0, 0.01], [102.0, 1.0]],
        "bids": [[100.0, 1.0]],
    })
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "BUY", "score": 5,
        "price": 100.5, "bid": 100.0, "ask": 101.0,
        "spread_bps": 20, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]

    engine.start()
    actions = engine.cycle()

    assert len(actions) == 1
    assert engine.paper.positions["XBTEUR"]["avg_price"] == pytest.approx(101.796)
    trade = db.execute("SELECT price,qty,quote FROM trades WHERE side='Buy'").fetchone()
    assert trade["price"] == pytest.approx(101.8)
    assert trade["quote"] < 5.0
    assert trade["qty"] == pytest.approx(trade["quote"] / trade["price"])
    db.close()


def test_paper_buy_skips_when_visible_ask_depth_cannot_cover_full_size(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.exchange = OrderBookExchange({
        "asks": [[101.0, 0.0001]],
        "bids": [[100.0, 1.0]],
    })
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "BUY", "score": 5,
        "price": 100.5, "bid": 100.0, "ask": 101.0,
        "spread_bps": 20, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]

    engine.start()
    actions = engine.cycle()

    assert engine.paper.positions == {}
    assert actions == []
    assert db.execute("SELECT COUNT(*) FROM trades WHERE side='Buy'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM audit WHERE event='ENTRY_SKIPPED'").fetchone()[0] == 1
    db.close()


def test_paper_sell_uses_visible_bid_depth_vwap_without_double_slippage(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5.0, 100.0, quote_to_eur_rate=1.0)
    engine.exchange = OrderBookExchange({
        "asks": [[102.1, 1.0]],
        "bids": [[102.0, 0.01], [101.0, 1.0]],
    })
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "SELL", "score": 0,
        "price": 102.0, "bid": 102.0, "ask": 102.1,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": ["strategy exit"],
    }]

    engine.start()
    actions = engine.cycle()

    assert any("XBTEUR SELL SIGNAL_SELL" in action for action in actions)
    assert "XBTEUR" not in engine.paper.positions
    trade = db.execute("SELECT price FROM trades WHERE side='Sell'").fetchone()
    assert trade["price"] == pytest.approx(101.2)
    db.close()


def test_paper_sell_stays_open_when_visible_bid_depth_is_insufficient(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.buy("XBTEUR", 5.0, 100.0, quote_to_eur_rate=1.0)
    engine.exchange = OrderBookExchange({
        "asks": [[102.1, 1.0]],
        "bids": [[102.0, 0.001]],
    })
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "SELL", "score": 0,
        "price": 102.0, "bid": 102.0, "ask": 102.1,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": ["strategy exit"],
    }]

    engine.start()
    actions = engine.cycle()

    assert "XBTEUR" in engine.paper.positions
    assert any("visible depth insufficient" in action for action in actions)
    assert db.execute("SELECT COUNT(*) FROM trades WHERE side='Sell'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM audit WHERE event='PAPER_EXIT_SKIPPED'").fetchone()[0] == 1
    db.close()