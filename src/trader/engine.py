from datetime import datetime, timezone
from .risk import Limits, check_order
from .indicators import analyze
from .db import audit, save_trade, get_state, set_state


class Engine:
    def __init__(self, settings, db, exchange, paper):
        self.s, self.db, self.exchange, self.paper = settings, db, exchange, paper
        # A restart must never silently resume simulated trading.
        self.paused = True
        self.scan_interval_seconds = settings.scan_interval
        self.last_scan_at = None
        self.last_prices = {}
        self.last_signals = []
        self.market_pairs = []
        self.market_ticker_count = 0
        self.limits = Limits(settings.max_trade, settings.max_daily_loss, settings.max_positions,
                             settings.max_spread_bps, settings.min_turnover)
        self._ensure_day()

    def _ensure_day(self):
        day = datetime.now(timezone.utc).date().isoformat()
        state = get_state(self.db, "daily", {})
        if state.get("date") != day:
            set_state(self.db, "daily", {"date": day, "start_equity": self.paper.equity({}), "realized": 0.0})

    def start(self):
        self.paused = False
        audit(self.db, "PAPER_STARTED", "Automatic paper trading enabled")

    def stop(self, reason="manual"):
        self.paused = True
        self.scan_interval_seconds = self.s.scan_interval
        audit(self.db, "PAPER_STOPPED", reason)

    def daily_pnl(self, prices=None):
        self._ensure_day()
        state = get_state(self.db, "daily", {})
        prices = prices or {}
        return self.paper.equity(prices) - float(state.get("start_equity", self.paper.cash))

    def scan_one(self, symbol, tickers=None):
        tickers = tickers if tickers is not None else self.exchange.tickers([symbol])
        t = next((x for x in tickers if x["symbol"] == symbol), None)
        if not t:
            return {"symbol": symbol, "signal": "HOLD", "reasons": ["Ticker unavailable"]}
        bid, ask = float(t.get("bid1Price") or 0), float(t.get("ask1Price") or 0)
        last = float(t.get("lastPrice") or 0)
        spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid > 0 and ask > 0 else 999999
        turnover = float(t.get("turnover24h") or 0)
        try:
            result = analyze(self.exchange.candles(symbol))
        except Exception as exc:
            return {
                "symbol": symbol, "signal": "HOLD", "score": 0,
                "price": last, "spread_bps": spread, "turnover": turnover,
                "ohlc_analyzed": False,
                "reasons": [f"Candle data unavailable: {type(exc).__name__}"],
            }
        result.update(symbol=symbol, price=last or result.get("price", 0), spread_bps=spread, turnover=turnover,
                      ohlc_analyzed=True)
        if spread > self.s.max_spread_bps:
            result["signal"] = "HOLD"
            result["reasons"].append("Spread too wide")
        if turnover < self.s.min_turnover:
            result["signal"] = "HOLD"
            result["reasons"].append("Turnover too low")
        return result

    def scan(self):
        # Modern Kraken client: discover all online spot pairs and fetch the
        # complete ticker snapshot with one public API request.
        if hasattr(self.exchange, "discover_pairs"):
            pairs = self.exchange.discover_pairs()
            tickers = self.exchange.tickers()
            self.market_pairs = pairs
            self.market_ticker_count = len(tickers)
            ticker_by_symbol = {item["symbol"]: item for item in tickers}
            quote_currency = getattr(self.s, "trade_quote_currency", "EUR").upper()
            analysis_limit = max(1, int(getattr(self.s, "max_candle_pairs", 10)))

            eligible = []
            results_by_symbol = {}
            for pair in pairs:
                symbol = pair["symbol"]
                ticker = ticker_by_symbol.get(symbol)
                if not ticker:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": 0, "spread_bps": 999999, "turnover": 0,
                        "quote": pair.get("quote"),
                        "reasons": ["Ticker unavailable"],
                    }
                    continue
                quote = str(pair.get("quote") or "").upper()
                bid, ask = float(ticker.get("bid1Price") or 0), float(ticker.get("ask1Price") or 0)
                spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid > 0 and ask > 0 else 999999
                turnover = float(ticker.get("turnover24h") or 0)
                if quote != quote_currency:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "spread_bps": spread, "turnover": turnover, "quote": quote,
                        "reasons": [f"Monitored only; PAPER cash is {quote_currency}"],
                    }
                elif spread > self.s.max_spread_bps:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "spread_bps": spread, "turnover": turnover, "quote": quote,
                        "reasons": ["Spread too wide"],
                    }
                elif turnover < self.s.min_turnover:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "spread_bps": spread, "turnover": turnover, "quote": quote,
                        "reasons": ["Turnover too low"],
                    }
                else:
                    eligible.append((turnover, symbol))

            # Tickers cover every Kraken spot market. To respect public API
            # limits, OHLC/indicator analysis is budgeted to the most liquid
            # EUR pairs each cycle; the complete universe is re-ranked each scan.
            eligible.sort(reverse=True)
            selected = {symbol for _, symbol in eligible[:analysis_limit]}
            for _, symbol in eligible:
                if symbol in selected:
                    result = self.scan_one(symbol, [ticker_by_symbol[symbol]])
                    result["quote"] = quote_currency
                    results_by_symbol[symbol] = result
                else:
                    ticker = ticker_by_symbol[symbol]
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "spread_bps": (float(ticker["ask1Price"]) - float(ticker["bid1Price"])) /
                                      ((float(ticker["ask1Price"]) + float(ticker["bid1Price"])) / 2) * 10000,
                        "turnover": float(ticker.get("turnover24h") or 0),
                        "quote": quote_currency,
                        "reasons": ["Ticker monitored; OHLC analysis deferred by scan budget"],
                    }
            return [results_by_symbol[pair["symbol"]] for pair in pairs if pair["symbol"] in results_by_symbol]

        # Compatibility path for small test doubles or explicitly configured watchlists.
        symbols = list(getattr(self.s, "watchlist", ()))
        tickers = self.exchange.tickers(symbols)
        self.market_pairs = [{"symbol": symbol, "quote": "EUR"} for symbol in symbols]
        self.market_ticker_count = len(tickers)
        return [self.scan_one(symbol, tickers) for symbol in symbols]

    def cycle(self):
        if self.paused:
            return []
        self._ensure_day()
        try:
            results = self.scan()
            self.last_scan_at = datetime.now(timezone.utc)
            self.last_prices = {x["symbol"]: float(x.get("price") or 0) for x in results if float(x.get("price") or 0) > 0}
            self.last_signals = results
        except Exception as exc:
            # Never trade on partial or stale market data after an exchange failure.
            audit(self.db, "SCAN_FAILED", f"{type(exc).__name__}: {exc}")
            self.stop("Market data scan failed")
            return ["SCAN_FAILED — paper trading paused"]
        prices = {x["symbol"]: x.get("price", 0) for x in results if x.get("price", 0) > 0}
        actions = []
        # Risk exits are checked before new entries.
        for symbol, pos in list(self.paper.positions.items()):
            price = prices.get(symbol)
            if not price:
                continue
            entry = pos["avg_price"]
            change_pct = (price / entry - 1) * 100
            reason = "STOP_LOSS" if change_pct <= -self.s.stop_loss_pct else "TAKE_PROFIT" if change_pct >= self.s.take_profit_pct else None
            if reason:
                fill = self.paper.sell(symbol, pos["qty"], price)
                save_trade(self.db, "PAPER", symbol, "Sell", fill["qty"], fill["quote"], fill["fee"], price, "FILLED")
                audit(self.db, reason, f"{symbol} change={change_pct:.3f}%")
                actions.append(f"{symbol} SELL {reason} {change_pct:.2f}%")
        if self.daily_pnl(prices) <= -self.s.max_daily_loss:
            self.stop("Daily loss limit reached")
            return actions + ["DAILY LOSS LIMIT — paused"]
        for signal in results:
            # Only EUR-quoted pairs can be bought with the EUR-denominated PAPER wallet.
            if signal.get("quote", "EUR") != getattr(self.s, "trade_quote_currency", "EUR").upper():
                continue
            if signal.get("signal") != "BUY" or signal["symbol"] in self.paper.positions:
                continue
            if len(self.paper.positions) >= self.s.max_positions:
                break
            try:
                check_order(self.s.max_trade, signal["spread_bps"], signal["turnover"], len(self.paper.positions),
                            self.daily_pnl(prices), self.limits)
                fill = self.paper.buy(signal["symbol"], self.s.max_trade, signal["price"])
                save_trade(self.db, "PAPER", signal["symbol"], "Buy", fill["qty"], fill["quote"], fill["fee"], fill["price"], "FILLED")
                actions.append(f"{signal['symbol']} BUY {fill['qty']:.8f} @ {fill['price']}")
            except (ValueError, PermissionError) as exc:
                audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: {exc}")
        return actions
