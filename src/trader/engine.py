from datetime import datetime, timezone
import logging
import math
import time
from .risk import Limits, check_order
from .indicators import analyze, confirm_multi_timeframe
from .learning import extract_features, predict_probability
from .db import audit, save_trade, get_state, set_state, save_paper_snapshot, record_shadow_prediction, settle_shadow_predictions


log = logging.getLogger(__name__)


FX_CURRENCIES = frozenset({
    "EUR", "USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "TRY",
    "MXN", "BRL", "SGD", "HKD", "RUB",
    "USDT", "USDC", "DAI", "TUSD", "USDP", "PYUSD", "EURT", "EURC",
})


def select_ohlc_markets(eligible, analysis_limit, cursor=0, priority_symbols=()):
    """Select priority markets first, then rotate fairly through the remaining universe.

    eligible is a sequence of (turnover_eur, symbol, quote_currency) tuples.
    Priority symbols are checked every cycle (open positions, explicit watchlist,
    and explicitly requested markets such as TON); all remaining capacity rotates.
    """
    markets = list(eligible or ())
    if not markets:
        return set(), 0, 0
    limit = max(1, int(analysis_limit))
    cursor = int(cursor or 0) % len(markets)
    by_symbol = {str(symbol).upper(): (symbol, quote) for _, symbol, quote in markets}
    selected = set()
    for raw_symbol in priority_symbols or ():
        symbol = str(raw_symbol or "").strip().upper().replace("/", "")
        match = by_symbol.get(symbol)
        if match and match not in selected:
            selected.add(match)
            if len(selected) >= limit:
                break
    priority_count = len(selected)
    ordered = markets[cursor:] + markets[:cursor]
    for _, symbol, quote in ordered:
        if len(selected) >= limit:
            break
        selected.add((symbol, quote))
    next_cursor = (cursor + limit) % len(markets)
    return selected, next_cursor, priority_count


def fresh_fx_rates_from_market(settings, market_pairs, ticker_data, prices):
    """Build conservative EUR marks from fresh, liquid direct EUR spot pairs."""
    rates = {"EUR": 1.0}
    candidates = {}
    for pair in market_pairs:
        symbol = pair.get("symbol")
        base = str(pair.get("base") or "").upper()
        quote = str(pair.get("quote") or "").upper()
        raw_price = prices.get(symbol) if symbol else None
        if not symbol or not base or not quote or raw_price is None:
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue

        ticker = ticker_data.get(symbol)
        turnover_eur = 0.0
        bid = ask = 0.0
        if ticker is not None:
            try:
                bid = float(ticker.get("bid1Price") or 0)
                ask = float(ticker.get("ask1Price") or 0)
                turnover = float(ticker.get("turnover24h") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if bid <= 0 or ask <= 0 or ask < bid or turnover <= 0:
                continue
            spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10000.0
            if not math.isfinite(spread_bps) or spread_bps > settings.max_spread_bps:
                continue
            if quote == "EUR":
                turnover_eur = turnover
            elif base == "EUR":
                turnover_eur = turnover / price
            if turnover_eur < max(1000.0, settings.min_turnover * 0.1):
                continue

        if quote == "EUR":
            currency, rate = base, (bid if ticker is not None else price)
        elif base == "EUR":
            currency, rate = quote, (1.0 / ask if ticker is not None else 1.0 / price)
        else:
            continue
        if not math.isfinite(rate) or rate <= 0:
            continue
        previous = candidates.get(currency)
        if previous is None or turnover_eur > previous[0]:
            candidates[currency] = (turnover_eur, rate)

    for currency, (_, rate) in candidates.items():
        rates[currency] = rate
    return rates


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
        self.last_ticker_data = {}
        self.limits = Limits(settings.max_trade, settings.max_daily_loss, settings.max_positions,
                             settings.max_spread_bps, settings.min_turnover)
        self._ensure_day()

    def _slippage_rate(self):
        """Conservative PAPER execution slippage as a decimal fraction."""
        value = float(getattr(self.s, "paper_slippage_bps", 5.0))
        if not math.isfinite(value) or value < 0 or value > 100:
            raise ValueError("PAPER slippage must be finite and between 0 and 100 bps")
        return value / 10000.0

    def _paper_buy_price(self, ask):
        price = float(ask) * (1.0 + self._slippage_rate())
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid PAPER buy fill price")
        return price

    def _paper_sell_price(self, bid):
        price = float(bid) * (1.0 - self._slippage_rate())
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid PAPER sell fill price")
        return price

    def _paper_trade_eur_values(self, quote_currency, fee, prices, *, position=None, qty=None, exit_price=None):
        """Convert PAPER fees and realized round-trip PnL to EUR using fresh FX only."""
        currency = str(quote_currency or "EUR").upper()
        rate = self._fresh_fx_rates(prices).get(currency)
        if rate is None:
            return None, None
        rate = float(rate)
        fee_value = float(fee)
        if not math.isfinite(rate) or rate <= 0 or not math.isfinite(fee_value) or fee_value < 0:
            return None, None
        fee_eur = fee_value * rate
        realized_pnl_eur = None
        if position is not None and qty is not None and exit_price is not None:
            quantity = float(qty)
            entry_price = float(position["avg_price"])
            exit_value = float(exit_price)
            if not all(math.isfinite(value) and value > 0 for value in (quantity, entry_price, exit_value)):
                raise ValueError("Invalid PAPER realized PnL inputs")
            entry_fee = quantity * entry_price * self.paper.fee_rate
            exit_fee = quantity * exit_value * self.paper.fee_rate
            realized_pnl_eur = (quantity * (exit_value - entry_price) - entry_fee - exit_fee) * rate
            if not math.isfinite(realized_pnl_eur):
                raise ValueError("PAPER realized EUR PnL is not finite")
        return fee_eur, realized_pnl_eur

    def _equity_after_liquidation(self, prices=None, fx_rates=None):
        """Estimate net liquidation value after exit fee and slippage on holdings."""
        prices = self.last_prices if prices is None else prices
        rates = self._fx_rates(prices) if fx_rates is None else fx_rates
        cash_value = sum(
            float(amount) * float(rates.get(currency, 0.0))
            for currency, amount in self.paper.cash_by_currency.items()
        )
        retention = (1.0 - self._slippage_rate()) * (1.0 - self.paper.fee_rate)
        position_value = 0.0
        for symbol, position in self.paper.positions.items():
            mark = float(prices.get(symbol) or 0.0)
            quote = str(position.get("quote_currency", "EUR")).upper()
            rate = float(rates.get(quote, 0.0))
            if not all(math.isfinite(x) and x >= 0 for x in (mark, rate)):
                raise ValueError("Invalid PAPER liquidation valuation")
            position_value += float(position["qty"]) * mark * retention * rate
        equity = cash_value + position_value
        if not math.isfinite(equity) or equity < 0:
            raise ValueError("PAPER liquidation equity is invalid")
        return equity

    def _ensure_day(self, prices=None):
        """Start a UTC trading day only after fresh market marks are available."""
        day = datetime.now(timezone.utc).date().isoformat()
        state = get_state(self.db, "daily", {})
        if state.get("date") != day:
            if prices is None:
                return False
            rates = self._fx_rates(prices)
            start_equity = self._equity_after_liquidation(prices, rates)
            set_state(self.db, "daily", {
                "date": day,
                "start_equity": start_equity,
                "realized": 0.0,
            })
        return True

    def start(self):
        self.paused = False
        audit(self.db, "PAPER_STARTED", "Automatic paper trading enabled")

    def stop(self, reason="manual"):
        self.paused = True
        self.scan_interval_seconds = self.s.scan_interval
        audit(self.db, "PAPER_STOPPED", reason)

    def _fresh_fx_rates(self, prices=None):
        """Build EUR valuation rates from direct, fresh EUR markets only."""
        prices = self.last_prices if prices is None else prices
        return fresh_fx_rates_from_market(
            self.s, self.market_pairs, self.last_ticker_data, prices,
        )

    def _fresh_fx_buy_rates(self, prices=None):
        """Return target-currency units obtainable per EUR at executable sides.

        These rates are deliberately separate from valuation rates: buying a
        currency crosses the ask on BASE/EUR and sells EUR at bid on EUR/QUOTE.
        """
        prices = self.last_prices if prices is None else prices
        rates = {"EUR": 1.0}
        candidates = {}
        for pair in self.market_pairs:
            symbol = pair.get("symbol")
            base = str(pair.get("base") or "").upper()
            quote = str(pair.get("quote") or "").upper()
            raw_price = prices.get(symbol) if symbol else None
            if not symbol or not base or not quote or raw_price is None:
                continue
            try:
                price = float(raw_price)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            ticker = self.last_ticker_data.get(symbol)
            bid = ask = 0.0
            turnover_eur = 0.0
            if ticker is not None:
                try:
                    bid = float(ticker.get("bid1Price") or 0)
                    ask = float(ticker.get("ask1Price") or 0)
                    turnover = float(ticker.get("turnover24h") or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (not all(math.isfinite(value) and value > 0 for value in (bid, ask, turnover))
                        or ask < bid):
                    continue
                spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10000.0
                if not math.isfinite(spread_bps) or spread_bps > self.s.max_spread_bps:
                    continue
                turnover_eur = turnover if quote == "EUR" else turnover / price if base == "EUR" else 0.0
                if turnover_eur < max(1000.0, self.s.min_turnover * 0.1):
                    continue
            if quote == "EUR":
                currency, rate = base, (1.0 / ask if ticker is not None else 1.0 / price)
            elif base == "EUR":
                currency, rate = quote, (bid if ticker is not None else price)
            else:
                continue
            if not math.isfinite(rate) or rate <= 0:
                continue
            previous = candidates.get(currency)
            if previous is None or turnover_eur > previous[0]:
                candidates[currency] = (turnover_eur, rate)
        for currency, (_, rate) in candidates.items():
            rates[currency] = rate
        return rates

    def _fx_rates(self, prices=None):
        """Build valuation rates; saved rates are valuation-only fallbacks.

        New PAPER conversions must use _fresh_fx_rates so an old cached rate
        can never authorize a new trade.
        """
        rates = self._fresh_fx_rates(prices)
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

    def _save_cycle_snapshot(self, results, prices):
        """Persist successful PAPER telemetry after positions and cash are updated."""
        try:
            analyzed = sum(1 for item in results if item.get("ohlc_analyzed") is True)
            save_paper_snapshot(
                self.db,
                ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                equity_eur=self._equity_after_liquidation(prices, self._fx_rates(prices)),
                cash=self.paper.cash_by_currency,
                open_positions=len(self.paper.positions),
                analyzed_markets=analyzed,
                buy_signals=sum(1 for item in results if item.get("signal") == "BUY"),
            )
        except Exception as exc:
            audit(self.db, "PAPER_SNAPSHOT_FAILED", f"{type(exc).__name__}: {exc}")

    def _seed_multicurrency_wallet(self, fx_buy_rates):
        """Seed USD and USDT PAPER balances once using executable FX buy sides."""
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
            currency_per_eur = float(fx_buy_rates.get(currency) or 0)
            if currency_per_eur <= 0:
                continue
            amount_eur = min(base_eur * 0.25, self.paper.balance("EUR"))
            if amount_eur <= 0:
                continue
            conversion = self.paper.convert("EUR", currency, amount_eur, currency_per_eur)
            audit(self.db, "PAPER_WALLET_SEED",
                  f"{conversion['from']}->{conversion['to']} spent={conversion['spent']:.8f} "
                  f"received={conversion['received']:.8f} fee={conversion['fee']:.8f}")
            seeded[currency] = True
            set_state(self.db, "paper_seeded_currencies", seeded)

    def daily_pnl(self, prices=None):
        prices = self.last_prices if prices is None else prices or {}
        if not prices and self.paper.positions:
            return 0.0
        if not self._ensure_day(prices if prices else None):
            return 0.0
        state = get_state(self.db, "daily", {})
        return self._equity_after_liquidation(prices, self._fx_rates(prices)) - float(state.get("start_equity", self.paper.cash))

    def scan_one(self, symbol, tickers=None, fx_rates=None, quote_currency=None):
        tickers = tickers if tickers is not None else self.exchange.tickers([symbol])
        t = next((x for x in tickers if x["symbol"] == symbol), None)
        if not t:
            return {"symbol": symbol, "signal": "HOLD", "ohlc_analyzed": False, "reasons": ["Ticker unavailable"]}
        bid, ask = float(t.get("bid1Price") or 0), float(t.get("ask1Price") or 0)
        last = float(t.get("lastPrice") or 0)
        spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid > 0 and ask > 0 else 999999
        turnover = float(t.get("turnover24h") or 0)
        quote_currency = str(quote_currency or t.get("quote") or "EUR").upper()
        turnover_eur = turnover * (fx_rates or {"EUR": 1.0}).get(quote_currency, 0.0)
        try:
            try:
                base_candles = self.exchange.candles(symbol, interval="60", limit=120)
            except TypeError:
                # Backwards-compatible adapter for simple test doubles.
                base_candles = self.exchange.candles(symbol)
            result = analyze(base_candles)
            # Higher-timeframe requests are made only for potential entries,
            # limiting Kraken REST load while refusing unconfirmed BUY signals.
            if result.get("signal") == "BUY" and getattr(self.s, "enable_multi_timeframe", False):
                try:
                    candles_4h = self.exchange.candles(symbol, interval="240", limit=100)
                    candles_1d = self.exchange.candles(symbol, interval="1440", limit=100)
                    result = confirm_multi_timeframe(result, candles_4h, candles_1d)
                except Exception as mtf_exc:
                    result["signal"] = "HOLD"
                    result.setdefault("reasons", []).append(
                        f"Multi-timeframe confirmation unavailable: {type(mtf_exc).__name__}"
                    )
        except Exception as exc:
            return {
                "symbol": symbol, "signal": "HOLD", "score": 0,
                "price": last, "bid": bid, "ask": ask, "spread_bps": spread, "turnover": turnover, "turnover_eur": turnover_eur,
                "ohlc_analyzed": False,
                "reasons": [f"Candle data unavailable: {type(exc).__name__}"],
            }
        analysis_success = len(base_candles) >= 60 and result.get("market_regime") != "UNKNOWN"
        # Research candidates run in shadow only: their forecasts and later
        # realized outcomes are stored, but never modify BUY/HOLD decisions.
        try:
            candidate = get_state(self.db, "ml_candidate_" + str(symbol).upper(), {})
            model = candidate.get("model") if isinstance(candidate, dict) else None
            if model and len(base_candles) >= 61:
                settle_shadow_predictions(
                    self.db, symbol=symbol, candles=base_candles,
                    fee_rate=self.s.fee_rate, slippage_bps=5.0,
                )
                # Use the latest closed candle for features and the current
                # executable ask as the forward-test entry. Never backdate a
                # shadow prediction to a candle that has already opened/closed.
                signal_index = len(base_candles) - 1
                features = extract_features(base_candles, end=signal_index)
                if features is not None:
                    probability = predict_probability(model, features)
                    threshold = float(candidate.get("probability_threshold", 0.58))
                    horizon = int(candidate.get("horizon_candles", 3))
                    prediction_time = datetime.now(timezone.utc).timestamp()
                    record_shadow_prediction(
                        self.db, symbol=symbol, model_version=candidate.get("model_version", ""),
                        signal_ts=prediction_time,
                        target_ts=prediction_time + horizon * 3600,
                        probability=probability, threshold=threshold,
                        entry_price=ask or last,
                    )
                    result["ml_shadow_probability"] = probability
                    result["ml_shadow_threshold"] = threshold
                    result["ml_shadow_candidate"] = probability >= threshold
        except Exception as shadow_exc:
            audit(self.db, "ML_SHADOW_ERROR", f"{symbol}: {type(shadow_exc).__name__}: {shadow_exc}")
        result.update(ohlc_analyzed=analysis_success, symbol=symbol, price=last or result.get("price", 0), bid=bid, ask=ask, spread_bps=spread, turnover=turnover, turnover_eur=turnover_eur, quote=quote_currency)
        if spread > self.s.max_spread_bps:
            result["signal"] = "HOLD"
            result["reasons"].append("Spread too wide")
        if turnover_eur < self.s.min_turnover:
            result["signal"] = "HOLD"
            result["reasons"].append("Turnover too low")
        return result

    def scan(self, seed_wallet=False):
        scan_started = time.monotonic()
        discovery_seconds = 0.0
        ticker_seconds = 0.0
        fx_seconds = 0.0
        ohlc_seconds = 0.0
        ohlc_analyzed_count = 0

        # Modern Kraken client: discover all online spot pairs and fetch the
        # complete ticker snapshot with one public API request.
        if hasattr(self.exchange, "discover_pairs"):
            stage_started = time.monotonic()
            pairs = self.exchange.discover_pairs()
            discovery_seconds = time.monotonic() - stage_started
            stage_started = time.monotonic()
            tickers = self.exchange.tickers()
            ticker_seconds = time.monotonic() - stage_started
            self.market_pairs = pairs
            self.market_ticker_count = len(tickers)
            ticker_by_symbol = {item["symbol"]: item for item in tickers}
            self.last_ticker_data = ticker_by_symbol
            analysis_limit = max(1, int(getattr(self.s, "max_candle_pairs", 10)))

            snapshot_prices = {
                item["symbol"]: float(item.get("lastPrice") or 0)
                for item in tickers if float(item.get("lastPrice") or 0) > 0
            }
            stage_started = time.monotonic()
            fresh_fx_rates = self._fresh_fx_rates(snapshot_prices)
            fresh_fx_buy_rates = self._fresh_fx_buy_rates(snapshot_prices)
            fx_seconds = time.monotonic() - stage_started
            # Market inspection must be side-effect free. Seed the virtual
            # multi-currency portfolio only inside an active PAPER cycle and
            # only from executable ask/bid routes in this fresh ticker snapshot.
            if seed_wallet:
                self._seed_multicurrency_wallet(fresh_fx_buy_rates)
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
                        "ohlc_analyzed": False,
                        "reasons": ["Ticker unavailable"],
                    }
                    continue
                base = str(pair.get("base") or "").upper()
                quote = str(pair.get("quote") or "").upper()
                bid, ask = float(ticker.get("bid1Price") or 0), float(ticker.get("ask1Price") or 0)
                spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid > 0 and ask > 0 else 999999
                turnover = float(ticker.get("turnover24h") or 0)
                if base in FX_CURRENCIES and quote in FX_CURRENCIES:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask, "spread_bps": spread,
                        "turnover": turnover, "turnover_eur": 0.0, "quote": quote,
                        "ohlc_analyzed": False,
                        "reasons": ["Fiat/stablecoin conversion market monitored only"],
                    }
                    continue
                turnover_eur = turnover * fresh_fx_rates.get(quote, 0.0)
                # Analyze every liquid market with a fresh EUR valuation route,
                # even when its quote wallet is currently empty. A BUY may
                # autonomously fund that quote currency from another wallet.
                if not fresh_fx_rates.get(quote):
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask,
                        "spread_bps": spread, "turnover": turnover, "turnover_eur": 0.0, "quote": quote,
                        "ohlc_analyzed": False, "reasons": [f"Monitored only; no fresh EUR conversion route for {quote}"],
                    }
                elif spread > self.s.max_spread_bps:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask,
                        "spread_bps": spread, "turnover": turnover, "quote": quote,
                        "ohlc_analyzed": False, "reasons": ["Spread too wide"],
                    }
                elif turnover_eur < self.s.min_turnover:
                    results_by_symbol[symbol] = {
                        "symbol": symbol, "signal": "HOLD", "score": 0,
                        "price": float(ticker.get("lastPrice") or 0),
                        "bid": bid, "ask": ask,
                        "spread_bps": spread, "turnover": turnover, "quote": quote,
                        "ohlc_analyzed": False, "reasons": ["Turnover too low or no EUR conversion rate"],
                    }
                else:
                    eligible.append((turnover_eur, symbol, quote))

            # Monitor every Kraken spot market through tickers. Rotate the
            # limited OHLC budget across all funded quote currencies.
            eligible.sort(reverse=True)
            cursor = int(get_state(self.db, "analysis_cursor", 0) or 0)
            # Keep held positions and explicit user priorities fresh every
            # cycle. TON is an explicit requested market; all remaining slots
            # continue rotating fairly across the full eligible universe.
            priorities = (
                list(self.paper.positions)
                + list(getattr(self.s, "watchlist", ()))
                + ["TONEUR", "TONUSD"]
            )
            selected, next_cursor, priority_selected = select_ohlc_markets(
                eligible, analysis_limit, cursor, priorities,
            )
            if eligible:
                set_state(self.db, "analysis_cursor", next_cursor)
            for _, symbol, quote in eligible:
                if (symbol, quote) in selected:
                    stage_started = time.monotonic()
                    result = self.scan_one(symbol, [ticker_by_symbol[symbol]], fx_rates=fresh_fx_rates, quote_currency=quote)
                    ohlc_seconds += time.monotonic() - stage_started
                    ohlc_analyzed_count += int(bool(result.get("ohlc_analyzed")))
                    result["quote"] = quote
                    result["turnover_eur"] = float(result.get("turnover") or 0) * fresh_fx_rates.get(quote, 0.0)
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
                        "turnover_eur": float(ticker.get("turnover24h") or 0) * fresh_fx_rates.get(quote, 0.0),
                        "quote": quote,
                        "ohlc_analyzed": False, "reasons": ["Ticker monitored; OHLC analysis deferred by rotating scan budget"],
                    }
            total_seconds = time.monotonic() - scan_started
            log.info(
                "Market scan timing: pairs=%d tickers=%d eligible=%d OHLC_selected=%d "
                "OHLC_analyzed=%d priority_selected=%d discovery=%.2fs ticker=%.2fs fx=%.2fs "
                "OHLC=%.2fs total=%.2fs",
                len(pairs), len(tickers), len(eligible), len(selected),
                ohlc_analyzed_count, priority_selected, discovery_seconds, ticker_seconds,
                fx_seconds, ohlc_seconds, total_seconds,
            )
            return [results_by_symbol[pair["symbol"]] for pair in pairs if pair["symbol"] in results_by_symbol]

        # Compatibility path for small test doubles or explicitly configured watchlists.
        symbols = list(getattr(self.s, "watchlist", ()))
        stage_started = time.monotonic()
        tickers = self.exchange.tickers(symbols)
        ticker_seconds = time.monotonic() - stage_started
        self.market_pairs = [{"symbol": symbol, "quote": "EUR"} for symbol in symbols]
        self.market_ticker_count = len(tickers)
        results = []
        for symbol in symbols:
            stage_started = time.monotonic()
            results.append(self.scan_one(symbol, tickers))
            ohlc_seconds += time.monotonic() - stage_started
        total_seconds = time.monotonic() - scan_started
        log.info(
            "Market scan timing: pairs=%d tickers=%d eligible=%d OHLC_selected=%d "
            "OHLC_analyzed=%d discovery=%.2fs ticker=%.2fs fx=%.2fs "
            "OHLC=%.2fs total=%.2fs",
            len(symbols), len(tickers), len(symbols), len(symbols),
            sum(int(bool(item.get("ohlc_analyzed"))) for item in results),
            discovery_seconds, ticker_seconds, fx_seconds, ohlc_seconds, total_seconds,
        )
        return results

    def cycle(self):
        # Market monitoring continues while paused; only PAPER trading is gated.
        try:
            results = self.scan(seed_wallet=not self.paused)
            self.last_scan_at = datetime.now(timezone.utc)
            self.last_prices = {x["symbol"]: float(x.get("price") or 0) for x in results if float(x.get("price") or 0) > 0}
            self.last_signals = results
            set_state(self.db, "paper_fx_rates", self._fx_rates(self.last_prices))
            if self.paused:
                return []
        except Exception as exc:
            # Never trade on partial or stale market data after an exchange failure.
            audit(self.db, "SCAN_FAILED", f"{type(exc).__name__}: {exc}")
            self.stop("Market data scan failed")
            return ["SCAN_FAILED — paper trading paused"]
        prices = {str(x["symbol"]).upper(): x.get("price", 0) for x in results if x.get("price", 0) > 0}
        result_by_symbol = {str(item.get("symbol", "")).upper(): item for item in results}
        invalid_positions = []
        for held_symbol in self.paper.positions:
            quote = result_by_symbol.get(str(held_symbol).upper())
            try:
                bid = float(quote.get("bid") or 0) if quote else 0.0
                ask = float(quote.get("ask") or 0) if quote else 0.0
                last = float(prices.get(str(held_symbol).upper()) or 0)
            except (TypeError, ValueError, OverflowError):
                bid = ask = last = 0.0
            if (not all(math.isfinite(value) and value > 0 for value in (bid, ask, last))
                    or ask < bid):
                invalid_positions.append(str(held_symbol).upper())
            else:
                # Use executable bid as the conservative mark for long spot holdings.
                prices[str(held_symbol).upper()] = bid
        if invalid_positions:
            reason = "Missing or invalid executable bid/ask for open positions: " + ", ".join(sorted(invalid_positions))
            audit(self.db, "OPEN_POSITION_PRICE_MISSING", reason)
            self.stop(reason)
            return ["SCAN_FAILED — open position quote unavailable; PAPER paused"]
        fresh_rates = self._fresh_fx_rates(prices)
        required_currencies = {
            currency for currency, amount in self.paper.cash_by_currency.items()
            if float(amount) > 1e-12
        }
        required_currencies.update(
            str(position.get("quote_currency", "EUR")).upper()
            for position in self.paper.positions.values()
        )
        missing_fx = sorted(currency for currency in required_currencies if currency not in fresh_rates)
        # Establish the daily baseline before any exits execute, but only when
        # every currency held by the wallet has a fresh EUR valuation route.
        if not missing_fx:
            self._ensure_day(prices)
        actions = []
        # Risk exits are checked before new entries.
        for symbol, pos in list(self.paper.positions.items()):
            price = prices.get(symbol)
            if not price:
                continue
            entry = pos["avg_price"]
            quote_row = next((item for item in results if item.get("symbol") == symbol), {})
            # Stops and exits must use the executable bid, not the last-traded price.
            exit_price = self._paper_sell_price(float(quote_row.get("bid") or price))
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
                else "SIGNAL_SELL" if quote_row.get("signal") == "SELL"
                else None
            )
            if reason:
                position_before_exit = dict(pos)
                fill = self.paper.sell(symbol, pos["qty"], exit_price)
                fee_eur, realized_pnl_eur = self._paper_trade_eur_values(
                    position_before_exit.get("quote_currency", "EUR"), fill["fee"], prices,
                    position=position_before_exit, qty=fill["qty"], exit_price=fill["price"],
                )
                save_trade(
                    self.db, "PAPER", symbol, "Sell", fill["qty"], fill["quote"],
                    fill["fee"], fill["price"], "FILLED",
                    fee_eur=fee_eur, realized_pnl_eur=realized_pnl_eur,
                )
                realized_pct = (exit_price / entry - 1) * 100
                audit(
                    self.db, reason,
                    f"{symbol} entry={entry:.10g} peak={peak:.10g} "
                    f"exit={exit_price:.10g} change={realized_pct:.3f}%",
                )
                actions.append(f"{symbol} SELL {reason} {realized_pct:.2f}%")
        if missing_fx:
            reason = "Missing fresh EUR FX routes for: " + ", ".join(missing_fx)
            audit(self.db, "PAPER_FX_ROUTE_MISSING", reason)
            self.stop(reason)
            return actions + ["FX_ROUTE_MISSING — PAPER paused"]
        if self.daily_pnl(prices) <= -self.s.max_daily_loss:
            # A daily-loss kill switch must not leave underwater positions
            # unmanaged. Liquidate remaining PAPER holdings at executable bid,
            # then pause all further entries.
            for symbol, pos in list(self.paper.positions.items()):
                quote_row = result_by_symbol[str(symbol).upper()]
                exit_price = self._paper_sell_price(float(quote_row["bid"]))
                position_before_exit = dict(pos)
                fill = self.paper.sell(symbol, pos["qty"], exit_price)
                fee_eur, realized_pnl_eur = self._paper_trade_eur_values(
                    position_before_exit.get("quote_currency", "EUR"), fill["fee"], prices,
                    position=position_before_exit, qty=fill["qty"], exit_price=fill["price"],
                )
                save_trade(
                    self.db, "PAPER", symbol, "Sell", fill["qty"], fill["quote"],
                    fill["fee"], fill["price"], "FILLED",
                    fee_eur=fee_eur, realized_pnl_eur=realized_pnl_eur,
                )
                realized_pct = (exit_price / pos["avg_price"] - 1.0) * 100.0
                audit(
                    self.db, "DAILY_LOSS_LIQUIDATION",
                    f"{symbol} qty={fill['qty']:.10g} bid={exit_price:.10g} change={realized_pct:.3f}%",
                )
                actions.append(f"{symbol} SELL DAILY_LOSS_LIMIT {realized_pct:.2f}%")
            self._save_cycle_snapshot(results, prices)
            self.stop("Daily loss limit reached; remaining PAPER positions liquidated")
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
                bid = float(signal.get("bid") or 0)
                ask = float(signal.get("ask") or 0)
                mark = float(signal.get("price") or 0)
                if (not all(math.isfinite(value) and value > 0 for value in (bid, ask, mark))
                        or ask < bid):
                    audit(self.db, "ENTRY_SKIPPED", f"{signal.get('symbol')}: invalid or non-executable ticker")
                    continue
                rates = self._fx_rates(prices)
                fresh_rates = self._fresh_fx_rates(prices)
                fresh_buy_rates = self._fresh_fx_buy_rates(prices)
                equity_eur = self._equity_after_liquidation(prices, rates)
                atr_percent = max(0.0, float(signal.get("atr_percent") or 0.0))
                stop_distance_pct = max(float(self.s.stop_loss_pct), atr_percent * 1.5, 0.5)
                risk_budget_eur = equity_eur * float(getattr(self.s, "risk_per_trade_pct", 0.5)) / 100.0
                trade_budget_eur = min(float(self.s.max_trade), risk_budget_eur / (stop_distance_pct / 100.0))
                check_order(trade_budget_eur, signal["spread_bps"], signal.get("turnover_eur", signal["turnover"]), len(self.paper.positions),
                            self.daily_pnl(prices), self.limits)
                # Cached rates may value the portfolio but must never fund a new trade.
                quote_to_eur = fresh_rates.get(quote_currency)
                quote_units_per_eur = fresh_buy_rates.get(quote_currency)
                if (not quote_to_eur or quote_to_eur <= 0
                        or not quote_units_per_eur or quote_units_per_eur <= 0):
                    audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: no fresh executable EUR conversion route for {quote_currency}")
                    continue
                # Autonomously route existing PAPER cash into the market's
                # quote currency when needed. Convert only enough to reach the
                # per-trade EUR cap; never use leverage or create a negative balance.
                quote_balance = self.paper.balance(quote_currency)
                target_allocation = trade_budget_eur * quote_units_per_eur
                target_shortfall = max(0.0, target_allocation - quote_balance)
                if target_shortfall > 1e-12:
                    for source_currency in sorted(
                        (currency for currency, amount in self.paper.cash_by_currency.items()
                         if currency != quote_currency and amount > 1e-12),
                        key=lambda currency: (currency != "EUR", currency),
                    ):
                        source_to_eur = fresh_rates.get(source_currency)
                        if not source_to_eur or source_to_eur <= 0:
                            continue
                        # Conversion fee is charged in the destination currency.
                        # Route through EUR. Non-EUR to non-EUR conversion
                        # is two exchange legs and must pay the fee twice.
                        fee_retention = 1.0 - self.paper.fee_rate
                        fee_legs = 2 if source_currency != "EUR" and quote_currency != "EUR" else 1
                        conversion_rate = source_to_eur * quote_units_per_eur
                        source_needed = (
                            target_shortfall
                            / (fee_retention ** fee_legs * conversion_rate)
                        )
                        source_amount = min(self.paper.balance(source_currency), source_needed)
                        if source_amount <= 1e-12:
                            continue
                        if fee_legs == 2:
                            first_leg = self.paper.convert(
                                source_currency, "EUR", source_amount, source_to_eur,
                            )
                            second_leg = self.paper.convert(
                                "EUR", quote_currency, first_leg["received"], quote_units_per_eur,
                            )
                            conversion = {
                                "from": source_currency, "to": quote_currency,
                                "spent": source_amount, "received": second_leg["received"],
                                "fee": first_leg["fee"] + second_leg["fee"], "legs": 2,
                            }
                        elif source_currency == "EUR":
                            conversion = self.paper.convert(
                                "EUR", quote_currency, source_amount, quote_units_per_eur,
                            )
                        else:
                            conversion = self.paper.convert(
                                source_currency, "EUR", source_amount, source_to_eur,
                            )
                        audit(
                            self.db, "PAPER_FX_CONVERSION",
                            f"{conversion['from']}->{conversion['to']} legs={fee_legs} "
                            f"spent={conversion['spent']:.8f} received={conversion['received']:.8f} "
                            f"fees={conversion.get('fee', 0.0):.8f}",
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
                entry_price = self._paper_buy_price(float(signal.get("ask") or signal["price"]))
                fill = self.paper.buy(signal["symbol"], allocation, entry_price, currency=quote_currency)
                fee_eur, _ = self._paper_trade_eur_values(quote_currency, fill["fee"], prices)
                save_trade(
                    self.db, "PAPER", signal["symbol"], "Buy", fill["qty"],
                    fill["quote"], fill["fee"], fill["price"], "FILLED", fee_eur=fee_eur,
                )
                actions.append(f"{signal['symbol']} BUY {fill['qty']:.8f} @ {fill['price']}")
            except (ValueError, PermissionError) as exc:
                audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: {exc}")
        self._save_cycle_snapshot(results, prices)
        return actions