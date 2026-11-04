from datetime import datetime, timezone
import math
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
            saved_rates = get_state(self.db, "paper_fx_rates", {"EUR": 1.0})
            if not isinstance(saved_rates, dict):
                saved_rates = {"EUR": 1.0}
            set_state(self.db, "daily", {
                "date": day,
                "start_equity": self.paper.equity(self.last_prices, saved_rates),
                "realized": 0.0,
            })

    def start(self):
        self.paused = False
        audit(self.db, "PAPER_STARTED", "Automatic paper trading enabled")

    def stop(self, reason="manual"):
        self.paused = True
        self.scan_interval_seconds = self.s.scan_interval
        audit(self.db, "PAPER_STOPPED", reason)

    def _fx_rates(self, prices=None):
        """Build quote-currency to EUR conversion rates from discovered spot markets."""
        prices = prices or self.last_prices
        graph = {}
        for pair in self.market_pairs:
            base = str(pair.get("base") or "").upper()
            quote = str(pair.get("quote") or "").upper()
            symbol = pair.get("symbol")
            price = float(prices.get(symbol) or 0) if symbol else 0
            if not base or not quote or price <= 0:
                continue
            graph.setdefault(base, {})[quote] = price
            graph.setdefault(quote, {})[base] = 1.0 / price
        rates = {"EUR": 1.0}
        queue = ["EUR"]
        while queue:
            target = queue.pop(0)
            for source, edges in graph.items():
                if source in rates or target not in edges:
                    continue
                rates[source] = edges[target] * rates[target]
                queue.append(source)
        # After a restart the exchange universe has not been scanned yet.
        # Retain the last validated FX snapshot until fresh ticker routes exist.
        saved_rates = get_state(self.db, "paper_fx_rates", {})
        if isinstance(saved_rates, dict):
            for currency, raw_rate in saved_rates.items():
                try:
                    rate = float(raw_rate)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(rate) and rate > 0:
                    rates.setdefault(str(currency).upper(), rate)
        rates["EUR"] = 1.0
        return rates

    def _seed_multicurrency_wallet(self, fx_rates):
        """Seed USD and USDT PAPER balances once, using virtual EUR conversions."""
        seeded = get_state(self.db, "paper_seeded_currencies", {})
        if not isinstance(seeded, dict):
            seeded = {}
        base_eur = get_state(self.db, "paper_seed_base_eur")
        if base_eur is None:
            base_eur = self.paper.balance("EUR")
            set_state(self.db, "paper_seed_base_eur", base_eur)
        base_eur = float(base_eur or 0)
        if base_eur <= 0:
            return
        # Retain 50% in EUR; allocate 25% each to USD and USDT when a Kraken
        # market-derived conversion route exists. Every conversion is audited.
        for currency in ("USD", "USDT"):
            if seeded.get(currency) or self.paper.balance(currency) > 0:
                seeded[currency] = True
                continue
            currency_to_eur = float(fx_rates.get(currency) or 0)
            if currency_to_eur <= 0:
                continue
            amount_eur = min(base_eur * 0.25, self.paper.balance("EUR"))
            if amount_eur <= 0:
                continue
            conversion = self.paper.convert("EUR", currency, amount_eur, 1.0 / currency_to_eur)
            audit(self.db, "PAPER_WALLET_SEED",
                  f"{conversion['from']}->{conversion['to']} spent={conversion['spent']:.8f} "
                  f"received={conversion['received']:.8f} fee={conversion['fee']:.8f}")
            seeded[currency] = True
            set_state(self.db, "paper_seeded_currencies", seeded)

    def daily_pnl(self, prices=None):
        self._ensure_day()
        state = get_state(self.db, "daily", {})
        prices = prices or self.last_prices or {}
        return self.paper.equity(prices, self._fx_rates(prices)) - float(state.get("start_equity", self.paper.cash))

    def scan_one(self, symbol, tickers=None, fx_rates=None, quote_currency=None):
        tickers = tickers if tickers is not None else self.exchange.tickers([symbol])
        t = next((x for x in tickers if x["symbol"] == symbol), None)
        if not t:
            return {"symbol": symbol, "signal": "HOLD", "reasons": ["Ticker unavailable"]}
        bid, ask = float(t.get("bid1Price") or 0), float(t.get("ask1Price") or 0)
        last = float(t.get("lastPrice") or 0)
        spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid > 0 and ask > 0 else 999999
        turnover = float(t.get("turnover24h") or 0)
        quote_currency = str(quote_currency or t.get("quote") or "EUR").upper()
        turnover_eur = turnover * (fx_rates or {"EUR": 1.0}).get(quote_currency, 0.0)
        try:
            result = analyze(self.exchange.candles(symbol))
        except Exception as exc:
            return {
                "symbol": symbol, "signal": "HOLD", "score": 0,
                "price": last, "bid": bid, "ask": ask, "spread_bps": spread, "turnover": turnover, "turnover_eur": turnover_eur,
                "reasons": [f"Candle data unavailable: {type(exc).__name__}"],
            }
        result.update(symbol=symbol, price=last or result.get("price", 0), bid=bid, ask=ask, spread_bps=spread, turnover=turnover, turnover_eur=turnover_eur, quote=quote_currency)
        if spread > self.s.max_spread_bps:
            result["signal"] = "HOLD"
            result["reasons"].append("Spread too wide")
        if turnover_eur < self.s.min_turnover:
            result["signal"] = "HOLD"
            result["reasons"].append("Turnover too low")
        return result

    def scan(self, seed_wallet=False):
        # Modern Kraken client: discover all online spot pairs and fetch the
        # complete ticker snapshot with one public API request.
        if hasattr(self.exchange, "discover_pairs"):
            pairs = self.exchange.discover_pairs()
            tickers = self.exchange.tickers()
            self.market_pairs = pairs
            self.market_ticker_count = len(tickers)
            ticker_by_symbol = {item["symbol"]: item for item in tickers}
            analysis_limit = max(1, int(getattr(self.s, "max_candle_pairs", 10)))

            snapshot_prices = {
                item["symbol"]: float(item.get("lastPrice") or 0)
                for item in tickers if float(item.get("lastPrice") or 0) > 0
            }
            fx_rates = self._fx_rates(snapshot_prices)
            # Market inspection must be side-effect free. Seed the virtual
            # multi-currency portfolio only inside an active PAPER cycle.
            if seed_wallet:
                self._seed_multicurrency_wallet(fx_rates)
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
                turnover_eur = turnover * fx_rates.get(quote, 0.0)
                # Analyze every liquid market with an EUR valuation route,
                # even when its quote wallet is currently empty. A BUY may
                # autonomously fund that quote currency from another wallet.
                if not fx_rates.get(quote):
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask,
                        "spread_bps": spread, "turnover": turnover, "turnover_eur": 0.0, "quote": quote,
                        "reasons": [f"Monitored only; no EUR conversion route for {quote}"],
                    }
                elif spread > self.s.max_spread_bps:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask,
                        "spread_bps": spread, "turnover": turnover, "quote": quote,
                        "reasons": ["Spread too wide"],
                    }
                elif turnover_eur < self.s.min_turnover:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask,
                        "spread_bps": spread, "turnover": turnover, "quote": quote,
                        "reasons": ["Turnover too low or no EUR conversion rate"],
                    }
                else:
                    eligible.append((turnover_eur, symbol, quote))

            # Monitor every Kraken spot market through tickers. Rotate the
            # limited OHLC budget across all funded quote currencies.
            eligible.sort(reverse=True)
            cursor = int(get_state(self.db, "analysis_cursor", 0) or 0) % max(1, len(eligible))
            ordered = eligible[cursor:] + eligible[:cursor]
            selected = {(symbol, quote) for _, symbol, quote in ordered[:analysis_limit]}
            if eligible:
                set_state(self.db, "analysis_cursor", (cursor + analysis_limit) % len(eligible))
            for _, symbol, quote in eligible:
                if (symbol, quote) in selected:
                    result = self.scan_one(symbol, [ticker_by_symbol[symbol]], fx_rates=fx_rates, quote_currency=quote)
                    result["quote"] = quote
                    result["turnover_eur"] = float(result.get("turnover") or 0) * fx_rates.get(quote, 0.0)
                    results_by_symbol[symbol] = result
                else:
                    ticker = ticker_by_symbol[symbol]
                    bid = float(ticker.get("bid1Price") or 0)
                    ask = float(ticker.get("ask1Price") or 0)
                    spread = ((ask - bid) / ((ask + bid) / 2) * 10000
                              if bid > 0 and ask > 0 else 999999)
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask,
                        "spread_bps": spread,
                        "turnover": float(ticker.get("turnover24h") or 0),
                        "turnover_eur": float(ticker.get("turnover24h") or 0) * fx_rates.get(quote, 0.0),
                        "quote": quote,
                        "reasons": ["Ticker monitored; OHLC analysis deferred by rotating scan budget"],
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
            results = self.scan(seed_wallet=True)
            self.last_scan_at = datetime.now(timezone.utc)
            self.last_prices = {x["symbol"]: float(x.get("price") or 0) for x in results if float(x.get("price") or 0) > 0}
            self.last_signals = results
            set_state(self.db, "paper_fx_rates", self._fx_rates(self.last_prices))
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
            quote_row = next((item for item in results if item.get("symbol") == symbol), {})
            # Stops and exits must use the executable bid, not the last-traded price.
            exit_price = float(quote_row.get("bid") or price)
            peak = self.paper.update_peak(symbol, price)
            change_pct = (exit_price / entry - 1) * 100
            peak_gain_pct = (peak / entry - 1) * 100
            trailing_pct = float(getattr(self.s, "trailing_stop_pct", 1.5))
            trailing_trigger = (
                peak_gain_pct > 0
                and exit_price <= peak * (1.0 - trailing_pct / 100.0)
            )
            reason = (
                "STOP_LOSS" if change_pct <= -self.s.stop_loss_pct
                else "TAKE_PROFIT" if change_pct >= self.s.take_profit_pct
                else "TRAILING_STOP" if trailing_trigger
                else None
            )
            if reason:
                fill = self.paper.sell(symbol, pos["qty"], exit_price)
                save_trade(self.db, "PAPER", symbol, "Sell", fill["qty"], fill["quote"], fill["fee"], fill["price"], "FILLED")
                realized_pct = (exit_price / entry - 1) * 100
                audit(
                    self.db, reason,
                    f"{symbol} entry={entry:.10g} peak={peak:.10g} "
                    f"exit={price:.10g} change={realized_pct:.3f}%",
                )
                actions.append(f"{symbol} SELL {reason} {realized_pct:.2f}%")
        if self.daily_pnl(prices) <= -self.s.max_daily_loss:
            self.stop("Daily loss limit reached")
            return actions + ["DAILY LOSS LIMIT — paused"]
        # Rank BUY candidates by strategy score first, then liquidity in EUR.
        # This prevents exchange pair ordering from deciding capital allocation.
        buy_candidates = sorted(
            (item for item in results if item.get("signal") == "BUY"),
            key=lambda item: (
                float(item.get("score") or 0),
                float(item.get("turnover_eur") or 0),
            ),
            reverse=True,
        )
        for signal in buy_candidates:
            quote_currency = str(signal.get("quote", "EUR")).upper()
            if signal.get("signal") != "BUY" or signal["symbol"] in self.paper.positions:
                continue
            if len(self.paper.positions) >= self.s.max_positions:
                break
            try:
                check_order(self.s.max_trade, signal["spread_bps"], signal.get("turnover_eur", signal["turnover"]), len(self.paper.positions),
                            self.daily_pnl(prices), self.limits)
                quote_to_eur = self._fx_rates(prices).get(quote_currency)
                if not quote_to_eur or quote_to_eur <= 0:
                    audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: no EUR valuation rate for {quote_currency}")
                    continue
                # Autonomously route existing PAPER cash into the market's
                # quote currency when needed. Convert only enough to reach the
                # per-trade EUR cap; never use leverage or create a negative balance.
                quote_balance = self.paper.balance(quote_currency)
                target_allocation = float(self.s.max_trade) / quote_to_eur
                target_shortfall = max(0.0, target_allocation - quote_balance)
                if target_shortfall > 1e-12:
                    rates = self._fx_rates(prices)
                    for source_currency in sorted(
                        (currency for currency, amount in self.paper.cash_by_currency.items()
                         if currency != quote_currency and amount > 1e-12),
                        key=lambda currency: (currency != "EUR", currency),
                    ):
                        source_to_eur = rates.get(source_currency)
                        if not source_to_eur or source_to_eur <= 0:
                            continue
                        # Conversion fee is charged in the destination currency.
                        source_needed = (
                            target_shortfall / (1.0 - self.paper.fee_rate)
                            * quote_to_eur / source_to_eur
                        )
                        source_amount = min(self.paper.balance(source_currency), source_needed)
                        if source_amount <= 1e-12:
                            continue
                        conversion = self.paper.convert(
                            source_currency, quote_currency, source_amount,
                            source_to_eur / quote_to_eur,
                        )
                        audit(
                            self.db, "PAPER_FX_CONVERSION",
                            f"{conversion['from']}->{conversion['to']} "
                            f"spent={conversion['spent']:.8f} received={conversion['received']:.8f}",
                        )
                        quote_balance = self.paper.balance(quote_currency)
                        target_shortfall = max(0.0, target_allocation - quote_balance)
                        if target_shortfall <= 1e-12:
                            break
                allocation = min(
                    target_allocation,
                    quote_balance / (1.0 + self.paper.fee_rate),
                )
                if allocation <= 0:
                    audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: no PAPER funds in {quote_currency}")
                    continue
                # A PAPER buy crosses the spread and therefore fills at ask.
                entry_price = float(signal.get("ask") or signal["price"])
                fill = self.paper.buy(signal["symbol"], allocation, entry_price, currency=quote_currency)
                save_trade(self.db, "PAPER", signal["symbol"], "Buy", fill["qty"], fill["quote"], fill["fee"], fill["price"], "FILLED")
                actions.append(f"{signal['symbol']} BUY {fill['qty']:.8f} @ {fill['price']}")
            except (ValueError, PermissionError) as exc:
                audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: {exc}")
        return actions