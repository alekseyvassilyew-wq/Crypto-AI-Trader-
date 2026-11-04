from types import SimpleNamespace
from src.trader.db import connect, get_state, set_state
from src.trader.engine import Engine
from src.trader.paper import PaperBroker
import pytest


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
    fresh_prices = {"XBTEUR": 12.0}
    engine._ensure_day(fresh_prices)
    daily = get_state(db, "daily")
    assert daily["start_equity"] == engine.paper.equity(fresh_prices, engine._fx_rates(fresh_prices))
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


def test_engine_starts_paused_but_keeps_monitoring_without_seed_or_trading(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "BUY", "score": 5,
        "price": 10.0, "bid": 9.99, "ask": 10.01,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000, "reasons": [],
    }]
    before = dict(engine.paper.cash_by_currency)
    assert engine.paused is True
    assert engine.cycle() == []
    assert engine.last_scan_at is not None
    assert engine.last_signals
    assert engine.paper.cash_by_currency == before
    assert engine.paper.positions == {}
    db.close()


def test_full_market_scan_monitors_all_pairs_and_can_analyze_non_eur_markets(tmp_path):
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

    def fake_scan_one(symbol, tickers=None, **kwargs):
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
                {"symbol": "USDZEUR", "base": "USD", "quote": "EUR"},
            ]
        def tickers(self):
            return [
                {"symbol": "XBTEUR", "bid1Price": 99.95, "ask1Price": 100, "lastPrice": 99.98, "turnover24h": 50000},
                {"symbol": "ETHUSD", "bid1Price": 2999.5, "ask1Price": 3000, "lastPrice": 2999.8, "turnover24h": 40000},
                {"symbol": "USDZEUR", "bid1Price": 0.89, "ask1Price": 0.90, "lastPrice": 0.895, "turnover24h": 10000},
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
         "price": 0.9, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
        {"symbol": "ETHUSD", "quote": "USD", "signal": "BUY", "score": 10,
         "price": 3000, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
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
         "price": 0.9, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
        {"symbol": "ETHUSD", "quote": "USD", "signal": "BUY", "score": 10,
         "price": 3000, "spread_bps": 10, "turnover": 50000, "turnover_eur": 45000, "reasons": []},
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
    rates = {"EUR": 1.0, "USD": 0.9, "USDT": 0.9}
    engine._seed_multicurrency_wallet(rates)
    assert engine.paper.balance("EUR") == 10
    assert engine.paper.balance("USD") == pytest.approx(5 / 0.9 * (1 - engine.paper.fee_rate))
    assert engine.paper.balance("USDT") == pytest.approx(5 / 0.9 * (1 - engine.paper.fee_rate))
    before = dict(engine.paper.cash_by_currency)
    engine._seed_multicurrency_wallet(rates)
    assert engine.paper.cash_by_currency == before
    rows = db.execute("SELECT COUNT(*) AS n FROM audit WHERE event='PAPER_WALLET_SEED'").fetchone()
    assert rows["n"] == 2
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
                {"symbol": "USDZEUR", "bid1Price": 0.89, "ask1Price": 0.90, "lastPrice": 0.895, "turnover24h": 50000},
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
    engine.paper.buy('XBTEUR', 5, 10)
    engine.paper.update_peak('XBTEUR', 10.5)
    engine.scan = lambda **kwargs: [{'symbol': 'XBTEUR', 'quote': 'EUR', 'signal': 'HOLD', 'score': 0, 'price': 10.3, 'spread_bps': 10, 'turnover': 50000, 'turnover_eur': 50000, 'reasons': []}]
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
        {'symbol': 'LOWEUR', 'quote': 'EUR', 'signal': 'BUY', 'score': 2, 'price': 10, 'spread_bps': 10, 'turnover': 50000, 'turnover_eur': 50000, 'reasons': []},
        {'symbol': 'HIGHEUR', 'quote': 'EUR', 'signal': 'BUY', 'score': 5, 'price': 10, 'spread_bps': 10, 'turnover': 50000, 'turnover_eur': 50000, 'reasons': []},
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
                {'symbol': 'USDZEUR', 'bid1Price': 0.89, 'ask1Price': 0.90, 'lastPrice': 0.895, 'turnover24h': 50000},
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
    assert engine.paper.positions["XBTEUR"]["avg_price"] == pytest.approx(101.0)
    trade = db.execute("SELECT price FROM trades WHERE side='Buy'").fetchone()
    assert trade["price"] == pytest.approx(101.0)
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
    assert trade["price"] == pytest.approx(97.9)
    assert "XBTEUR" not in engine.paper.positions
    db.close()



def test_saved_fx_snapshot_values_non_eur_wallet_after_restart(tmp_path):
    engine, db = make_engine(tmp_path)
    engine.paper.cash_by_currency["USD"] = 10.0
    engine.paper.buy("ETHUSD", 5.0, 10.0, currency="USD")
    set_state(db, "paper_fx_rates", {"EUR": 1.0, "USD": 0.9})

    rates = engine._fx_rates({})

    assert rates["USD"] == pytest.approx(0.9)
    assert engine.paper.equity({}, rates) == pytest.approx(
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
    assert daily["start_equity"] == pytest.approx(14.987 + 0.5 * 9.7)
    assert engine.daily_pnl(engine.last_prices) < 0
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
    engine.paper.buy("XBTEUR", 5, 100.0)
    engine.scan = lambda **kwargs: [{
        "symbol": "XBTEUR", "quote": "EUR", "signal": "SELL", "score": 0,
        "price": 101.0, "bid": 100.5, "ask": 100.6,
        "spread_bps": 10, "turnover": 50000, "turnover_eur": 50000,
        "reasons": ["strategy exit"],
    }]
    engine.start()

    actions = engine.cycle()

    assert any("XBTEUR SELL SIGNAL_SELL" in action for action in actions)
    assert "XBTEUR" not in engine.paper.positions
    trade = db.execute("SELECT side,price FROM trades WHERE side='Sell' ORDER BY id DESC LIMIT 1").fetchone()
    assert trade["side"] == "Sell"
    assert trade["price"] == pytest.approx(100.5)
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
    assert engine.daily_pnl({"XBTEUR": 99.0}) == pytest.approx(
        engine.paper.equity({"XBTEUR": 99.0}, engine._fx_rates({"XBTEUR": 99.0}))
        - get_state(db, "daily")["start_equity"]
    )
    db.close()
