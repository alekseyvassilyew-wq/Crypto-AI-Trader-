from datetime import datetime, timezone
from .risk import Limits, check_order
from .indicators import analyze
from .db import audit, save_trade, get_state, set_state

class Engine:
    def __init__(self, settings, db, exchange, paper):
        self.s, self.db, self.exchange, self.paper = settings, db, exchange, paper
        self.paused = True
        self.scan_interval_seconds = settings.scan_interval
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
        tickers = tickers if tickers is not None else self.exchange.tickers(self.s.watchlist)
        t = next((x for x in tickers if x["symbol"] == symbol), None)
        if not t: return {"symbol": symbol, "signal": "HOLD", "reasons": ["Ticker unavailable"]}
        bid, ask = float(t.get("bid1Price") or 0), float(t.get("ask1Price") or 0)
        last = float(t.get("lastPrice") or 0)
        spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid > 0 and ask > 0 else 999999
        turnover = float(t.get("turnover24h") or 0)
        result = analyze(self.exchange.candles(symbol))
        result.update(symbol=symbol, price=last or result.get("price", 0), spread_bps=spread, turnover=turnover)
        if spread > self.s.max_spread_bps: result["signal"] = "HOLD"; result["reasons"].append("Spread too wide")
        if turnover < self.s.min_turnover: result["signal"] = "HOLD"; result["reasons"].append("Turnover too low")
        return result

    def scan(self):
        tickers = self.exchange.tickers(self.s.watchlist)
        return [self.scan_one(symbol, tickers) for symbol in self.s.watchlist]

    def cycle(self):
        if self.paused: return []
        self._ensure_day()
        results = self.scan()
        prices = {x["symbol"]: x.get("price", 0) for x in results if x.get("price", 0) > 0}
        actions = []
        # Risk exits are checked before new entries.
        for symbol, pos in list(self.paper.positions.items()):
            price = prices.get(symbol)
            if not price: continue
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
            if signal.get("signal") != "BUY" or signal["symbol"] in self.paper.positions: continue
            if len(self.paper.positions) >= self.s.max_positions: break
            try:
                check_order(self.s.max_trade, signal["spread_bps"], signal["turnover"], len(self.paper.positions),
                            self.daily_pnl(prices), self.limits)
                fill = self.paper.buy(signal["symbol"], self.s.max_trade, signal["price"])
                save_trade(self.db, "PAPER", signal["symbol"], "Buy", fill["qty"], fill["quote"], fill["fee"], fill["price"], "FILLED")
                actions.append(f"{signal['symbol']} BUY {fill['qty']:.8f} @ {fill['price']}")
            except (ValueError, PermissionError) as exc:
                audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: {exc}")
        return actions
