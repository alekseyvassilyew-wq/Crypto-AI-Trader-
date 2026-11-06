from datetime import datetime, timezone
import logging
import math
import copy
import time
from .risk import Limits, check_order
from .indicators import analyze, confirm_multi_timeframe
from .learning import extract_features, predict_probability
from .strategy_ensemble import evaluate_strategy_ensemble
from .adaptive_strategy import evaluate_adaptive_strategy_weights
from .paper import estimate_book_vwap
from .db import audit, save_trade, save_paper_fill, save_paper_fx_conversion, backfill_paper_seed_fx_ledger, get_state, set_state, save_paper_snapshot, record_shadow_prediction, settle_shadow_predictions, record_strategy_shadow_prediction, settle_strategy_shadow_predictions, get_strategy_shadow_regime_metrics


log = logging.getLogger(__name__)


FX_CURRENCIES = frozenset({
    "EUR", "USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "TRY",
    "MXN", "BRL", "SGD", "HKD", "RUB",
    "USDT", "USDC", "DAI", "TUSD", "USDP", "PYUSD", "EURT", "EURC",
})



def adaptive_ohlc_budget(configured_limit, interval_seconds, avg_seconds_per_market, safety_fraction=0.55):
    """Bound sequential OHLC requests to leave headroom for the next scan."""
    if isinstance(configured_limit, bool) or not isinstance(configured_limit, int) or configured_limit < 1:
        raise ValueError("configured_limit must be a positive integer")
    values = (interval_seconds, avg_seconds_per_market, safety_fraction)
    if any(isinstance(value, bool) for value in values):
        raise ValueError("scan budget inputs must be numeric")
    try:
        interval, average, fraction = (float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("scan budget inputs must be finite numbers") from exc
    if not all(math.isfinite(value) for value in (interval, average, fraction)):
        raise ValueError("scan budget inputs must be finite numbers")
    if interval <= 0 or average <= 0 or not 0 < fraction < 1:
        raise ValueError("scan interval, average latency, and safety fraction are out of range")
    target_seconds = max(8.0, interval * fraction)
    return max(1, min(configured_limit, int(target_seconds / average)))


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
        seed_backfill = backfill_paper_seed_fx_ledger(db)
        if seed_backfill.get("unparsed", 0):
            log.warning(
                "PAPER seed FX ledger backfill incomplete: backfilled=%d already_present=%d unparsed=%d",
                seed_backfill.get("backfilled", 0),
                seed_backfill.get("already_present", 0),
                seed_backfill.get("unparsed", 0),
            )
        elif seed_backfill.get("backfilled", 0):
            log.info(
                "Recovered %d legacy PAPER seed FX conversion fee records from audit history",
                seed_backfill["backfilled"],
            )
        # Fresh installs auto-run PAPER; deliberate and safety pauses survive restarts.
        self.paused = bool(get_state(db, "paper_paused", False))
        self.scan_interval_seconds = settings.scan_interval
        self.last_scan_at = None
        self.market_snapshot_at = None
        saved_scan_metrics = get_state(db, "last_scan_metrics", {})
        self.last_scan_metrics = (
            saved_scan_metrics
            if isinstance(saved_scan_metrics, dict)
            and all(
                isinstance(saved_scan_metrics.get(key), (int, float))
                and not isinstance(saved_scan_metrics.get(key), bool)
                and math.isfinite(float(saved_scan_metrics[key]))
                and float(saved_scan_metrics[key]) >= 0
                for key in ("pairs", "tickers", "eligible", "ohlc_selected", "ohlc_budget", "ohlc_analyzed", "duration_seconds")
            )
            else {}
        )
        self.last_prices = {}
        self.last_signals = []
        self.market_pairs = []
        self.market_ticker_count = 0
        self.last_ticker_data = {}
        self.limits = Limits(settings.max_trade, settings.max_daily_loss, settings.max_positions,
                             settings.max_spread_bps, settings.min_turnover)
        self._ensure_day()
        # Closed OHLC candles are stable for their timeframe bucket. Cache
        # scan-time data only; command research/backtests still fetch directly.
        self._scan_candle_cache = {}
        self._scan_candle_cache_limit = 512
        self._scan_candle_cache_hits = 0
        self._scan_candle_cache_misses = 0

    def _scan_candles(self, symbol, interval="60", limit=120):
        """Reuse closed candles until the next UTC timeframe boundary."""
        minutes = int(interval)
        if minutes <= 0:
            raise ValueError("Candle interval must be positive")
        now = time.time()
        # Allow Kraken up to ten seconds to publish the final closed candle.
        cache_clock = max(0.0, now - 10.0)
        bucket = int(cache_clock // (minutes * 60))
        key = (str(symbol).upper(), str(interval), int(limit))
        cached = self._scan_candle_cache.get(key)
        if cached and cached[0] == bucket:
            self._scan_candle_cache_hits += 1
            self._scan_candle_cache.pop(key, None)
            self._scan_candle_cache[key] = cached
            return copy.deepcopy(cached[1])

        self._scan_candle_cache_misses += 1
        candles = self.exchange.candles(symbol, interval=str(interval), limit=int(limit))
        self._scan_candle_cache[key] = (bucket, copy.deepcopy(candles))
        # Remove stale timeframe buckets, then enforce a strict memory bound.
        for old_key, old_value in list(self._scan_candle_cache.items()):
            old_interval = old_key[1]
            if old_interval.isdigit() and old_value[0] < int(cache_clock // (int(old_interval) * 60)):
                self._scan_candle_cache.pop(old_key, None)
        while len(self._scan_candle_cache) > self._scan_candle_cache_limit:
            self._scan_candle_cache.pop(next(iter(self._scan_candle_cache)))
        return copy.deepcopy(candles)

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

    def _paper_book_fill(self, symbol, quantity, side, *, max_quote=None):
        """Estimate a full PAPER fill from live L2 depth; never extrapolate.

        Legacy exchange adapters without order_book retain the conservative
        bid/ask plus slippage model. Kraken uses visible depth and VWAP instead,
        so spread and depth impact are not charged a second time as slippage.
        """
        order_book = getattr(self.exchange, "order_book", None)
        if not callable(order_book):
            return None
        try:
            depth = getattr(self.s, "paper_order_book_depth", 10)
            book = order_book(symbol, depth=depth)
            if not isinstance(book, dict):
                raise ValueError("malformed order-book response")
            side_key = "asks" if side == "buy" else "bids" if side == "sell" else None
            if side_key is None:
                raise ValueError("side must be buy or sell")
            levels = book.get(side_key)
            if not isinstance(levels, (list, tuple)) or not levels:
                raise ValueError(f"empty {side_key} order-book")
            requested = float(quantity)
            if max_quote is not None:
                if side != "buy":
                    raise ValueError("max_quote is only valid for buys")
                budget = float(max_quote)
                worst_visible_price = float(levels[-1][0])
                if not math.isfinite(budget) or budget <= 0 or not math.isfinite(worst_visible_price) or worst_visible_price <= 0:
                    raise ValueError("invalid PAPER order-book quote budget")
                requested = min(requested, budget / worst_visible_price)
            estimate = estimate_book_vwap(levels, requested, side=side)
            audit(
                self.db, "PAPER_BOOK_VWAP_ESTIMATE",
                f"{symbol} side={side} qty={estimate['qty']:.12g} "
                f"quote={estimate['quote']:.12g} vwap={estimate['vwap']:.12g} "
                f"levels_used={estimate['levels_used']}",
            )
            return estimate
        except Exception as exc:
            raise ValueError(
                f"order-book {side} unavailable for {symbol}: {type(exc).__name__}: {exc}"
            ) from exc

    def _paper_trade_eur_values(self, quote_currency, fee, prices, *, position=None, qty=None, exit_price=None):
        """Value PAPER fees and realized PnL using entry-time and exit-time EUR FX."""
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
            held_quantity = float(position.get("qty", 0.0))
            exit_value = float(exit_price)
            cost_basis_eur = position.get("cost_basis_eur")
            entry_fees_eur = position.get("entry_fees_eur")
            if (
                not math.isfinite(quantity) or quantity <= 0
                or not math.isfinite(held_quantity) or held_quantity <= 0
                or quantity > held_quantity + 1e-12
                or not math.isfinite(exit_value) or exit_value <= 0
            ):
                raise ValueError("Invalid PAPER realized PnL inputs")
            # Legacy open positions have no historical FX cost basis; do not invent one.
            if cost_basis_eur is not None and entry_fees_eur is not None:
                fraction = min(1.0, quantity / held_quantity)
                allocated_basis = float(cost_basis_eur) * fraction
                allocated_entry_fees = float(entry_fees_eur) * fraction
                exit_gross_eur = quantity * exit_value * rate
                realized_pnl_eur = exit_gross_eur - fee_eur - allocated_basis - allocated_entry_fees
                if not all(math.isfinite(value) for value in (
                    allocated_basis, allocated_entry_fees, exit_gross_eur, realized_pnl_eur
                )):
                    raise ValueError("PAPER realized EUR PnL is not finite")
        return fee_eur, realized_pnl_eur

    def _equity_after_liquidation(self, prices=None, fx_rates=None):
        """Estimate net liquidation value after exit fee and slippage on holdings."""
        prices = self.last_prices if prices is None else prices
        rates = self._fx_rates(prices) if fx_rates is None else fx_rates
        cash_value = 0.0
        for currency, amount in self.paper.cash_by_currency.items():
            amount = float(amount)
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(f"Invalid PAPER cash balance for {currency}")
            if amount <= 0:
                continue
            rate = rates.get(currency)
            if rate is None or not math.isfinite(float(rate)) or float(rate) <= 0:
                raise ValueError(f"Missing or invalid PAPER cash FX valuation for {currency}")
            cash_value += amount * float(rate)
        retention = (1.0 - self._slippage_rate()) * (1.0 - self.paper.fee_rate)
        position_value = 0.0
        for symbol, position in self.paper.positions.items():
            raw_mark = prices.get(symbol)
            if raw_mark is None:
                raise ValueError(f"Missing current PAPER mark price for {symbol}")
            mark = float(raw_mark)
            quote = str(position.get("quote_currency", "EUR")).upper()
            raw_rate = rates.get(quote)
            if raw_rate is None:
                raise ValueError(f"Missing PAPER position FX valuation for {symbol} ({quote})")
            rate = float(raw_rate)
            if not all(math.isfinite(x) and x > 0 for x in (mark, rate)):
                raise ValueError("Invalid PAPER liquidation valuation")
            qty = float(position["qty"])
            if not math.isfinite(qty) or qty <= 0:
                raise ValueError(f"Invalid PAPER position quantity for {symbol}")
            position_value += qty * mark * retention * rate
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
            # Daily risk baselines must use current executable-market FX marks,
            # never a cached fallback that can conceal currency moves.
            rates = self._fresh_fx_rates(prices)
            start_equity = self._equity_after_liquidation(prices, rates)
            set_state(self.db, "daily", {
                "date": day,
                "start_equity": start_equity,
                "realized": 0.0,
            })
        return True

    def start(self):
        self.paused = False
        set_state(self.db, "paper_paused", False)
        audit(self.db, "PAPER_STARTED", "Automatic paper trading enabled")

    def stop(self, reason="manual"):
        self.paused = True
        set_state(self.db, "paper_paused", True)
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

    def _save_cycle_snapshot(self, results, prices, *, require_fresh_fx=False):
        """Persist PAPER telemetry only when every non-zero balance is valuated."""
        try:
            analyzed = sum(1 for item in results if item.get("ohlc_analyzed") is True)
            rates = self._fresh_fx_rates(prices) if require_fresh_fx else self._fx_rates(prices)
            if require_fresh_fx:
                required = {
                    str(currency).upper()
                    for currency, amount in self.paper.cash_by_currency.items()
                    if float(amount) > 1e-12
                }
                required.update(
                    str(position.get("quote_currency", "EUR")).upper()
                    for position in self.paper.positions.values()
                )
                missing_rates = sorted(currency for currency in required if currency not in rates)
                missing_prices = sorted(
                    symbol for symbol in self.paper.positions
                    if not prices.get(str(symbol).upper()) or float(prices.get(str(symbol).upper()) or 0) <= 0
                )
                if missing_rates or missing_prices:
                    raise ValueError(
                        "fresh PAPER valuation incomplete: "
                        f"missing FX={','.join(missing_rates) or 'none'}; "
                        f"missing prices={','.join(missing_prices) or 'none'}"
                    )
            save_paper_snapshot(
                self.db,
                ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                equity_eur=self._equity_after_liquidation(prices, rates),
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
            cash_before = dict(self.paper.cash_by_currency)
            basis_before = dict(self.paper.cash_cost_basis_eur)
            try:
                conversion = self.paper.convert(
                    "EUR", currency, amount_eur, currency_per_eur, persist=False,
                )
                # Seed conversions are part of the same accounting ledger as
                # later trade-driven conversions. The fee is charged in the
                # destination currency, so value it back to EUR at this exact
                # executable conversion rate.
                save_paper_fx_conversion(
                    self.db,
                    from_currency=conversion["from"], to_currency=conversion["to"],
                    spent=conversion["spent"], received=conversion["received"],
                    fee_amount=conversion["fee"], fee_currency=currency,
                    fee_eur=conversion["fee"] / currency_per_eur, legs=1,
                    cash_by_currency=self.paper.cash_by_currency,
                    cash_cost_basis_eur=self.paper.cash_cost_basis_eur,
                    positions=self.paper.positions,
                    audit_event="PAPER_WALLET_SEED",
                    audit_details=(
                        f"{conversion['from']}->{conversion['to']} spent={conversion['spent']:.8f} "
                        f"received={conversion['received']:.8f} fee={conversion['fee']:.8f}"
                    ),
                )
            except Exception:
                self.paper.cash_by_currency = cash_before
                self.paper.cash_cost_basis_eur = basis_before
                self.paper._sync_cash()
                raise
            seeded[currency] = True
            set_state(self.db, "paper_seeded_currencies", seeded)

    def daily_pnl(self, prices=None):
        prices = self.last_prices if prices is None else prices or {}
        if not prices and self.paper.positions:
            raise ValueError("Cannot calculate PAPER daily PnL: open positions have no current market marks")
        if not self._ensure_day(prices if prices else None):
            return 0.0
        state = get_state(self.db, "daily", {})
        # Risk gates require fresh FX for every non-EUR wallet balance and position.
        return self._equity_after_liquidation(prices, self._fresh_fx_rates(prices)) - float(state.get("start_equity", self.paper.cash))

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
                base_candles = self._scan_candles(symbol, interval="60", limit=120)
            except TypeError:
                # Backwards-compatible adapter for simple test doubles.
                base_candles = self.exchange.candles(symbol)
            result = analyze(base_candles)
            # Independent strategy opinions are collected in shadow mode only.
            # The existing production BUY/HOLD decision remains unchanged.
            result["strategy_ensemble_shadow"] = evaluate_strategy_ensemble(base_candles)
            # Track BUY opinions independently; this ledger is observational only.
            try:
                settle_strategy_shadow_predictions(
                    self.db, symbol=symbol, candles=base_candles,
                    fee_rate=self.s.fee_rate, slippage_bps=self._slippage_rate() * 10000.0,
                    interval_seconds=3600,
                )
                shadow = result["strategy_ensemble_shadow"]
                # Regime-aware weights are advisory shadow telemetry only.
                shadow["adaptive_weights"] = evaluate_adaptive_strategy_weights(
                    shadow.get("regime", "UNKNOWN"),
                    get_strategy_shadow_regime_metrics(self.db, symbol=symbol),
                )
                if shadow.get("mode") == "SHADOW_ONLY" and len(base_candles) >= 60:
                    closed_candle_ts = float(base_candles[-1][0]) + 3600.0
                    for strategy_name, opinion in shadow.get("strategies", {}).items():
                        if opinion.get("signal") == "BUY" and (ask or last) > 0:
                            record_strategy_shadow_prediction(
                                self.db, symbol=symbol, strategy=strategy_name,
                                signal_ts=closed_candle_ts,
                                target_ts=closed_candle_ts + 3 * 3600.0,
                                entry_price=ask or last, regime=shadow.get("regime", ""),
                            )
            except Exception as strategy_shadow_exc:
                audit(
                    self.db, "STRATEGY_SHADOW_ERROR",
                    f"{symbol}: {type(strategy_shadow_exc).__name__}: {strategy_shadow_exc}",
                )
            # Higher-timeframe requests are made only for potential entries,
            # limiting Kraken REST load while refusing unconfirmed BUY signals.
            if result.get("signal") == "BUY" and getattr(self.s, "enable_multi_timeframe", False):
                try:
                    candles_4h = self._scan_candles(symbol, interval="240", limit=100)
                    candles_1d = self._scan_candles(symbol, interval="1440", limit=100)
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
                    fee_rate=self.s.fee_rate, slippage_bps=self._slippage_rate() * 10000.0,
                    interval_seconds=3600,
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
        self._scan_candle_cache_hits = 0
        self._scan_candle_cache_misses = 0
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
            self.market_snapshot_at = datetime.now(timezone.utc)
            self.market_pairs = pairs
            self.market_ticker_count = len(tickers)
            ticker_by_symbol = {item["symbol"]: item for item in tickers}
            self.last_ticker_data = ticker_by_symbol
            configured_analysis_limit = max(1, int(getattr(self.s, "max_candle_pairs", 10)))
            # Persist latency across restarts. The conservative default protects
            # the first cycle; the EWMA adapts to Kraken response time thereafter.
            average_ohlc_seconds = get_state(self.db, "ohlc_avg_seconds_per_market", 3.0)
            try:
                average_ohlc_seconds = float(average_ohlc_seconds)
                if not math.isfinite(average_ohlc_seconds) or average_ohlc_seconds <= 0:
                    average_ohlc_seconds = 3.0
            except (TypeError, ValueError, OverflowError):
                average_ohlc_seconds = 3.0
            interval_seconds = getattr(self, "scan_interval_seconds", None)
            if interval_seconds is None:
                interval_seconds = getattr(self.s, "scan_interval", 60)
            analysis_limit = adaptive_ohlc_budget(
                configured_analysis_limit, interval_seconds, average_ohlc_seconds,
            )

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
            if selected and ohlc_seconds > 0:
                observed_average = ohlc_seconds / len(selected)
                if math.isfinite(observed_average) and observed_average > 0:
                    updated_average = 0.7 * average_ohlc_seconds + 0.3 * observed_average
                    set_state(self.db, "ohlc_avg_seconds_per_market", updated_average)
            total_seconds = time.monotonic() - scan_started
            self.last_scan_metrics = {
                "pairs": len(pairs),
                "tickers": len(tickers),
                "eligible": len(eligible),
                "ohlc_selected": len(selected),
                "ohlc_budget": analysis_limit,
                "ohlc_analyzed": ohlc_analyzed_count,
                "candle_cache_hits": self._scan_candle_cache_hits,
                "candle_cache_misses": self._scan_candle_cache_misses,
                "duration_seconds": total_seconds,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            set_state(self.db, "last_scan_metrics", self.last_scan_metrics)
            log.info(
                "Market scan timing: pairs=%d tickers=%d eligible=%d OHLC_selected=%d OHLC_budget=%d "
                "OHLC_analyzed=%d priority_selected=%d candle_cache_hits=%d candle_cache_misses=%d "
                "discovery=%.2fs ticker=%.2fs fx=%.2fs OHLC=%.2fs total=%.2fs",
                len(pairs), len(tickers), len(eligible), len(selected), analysis_limit,
                ohlc_analyzed_count, priority_selected, self._scan_candle_cache_hits,
                self._scan_candle_cache_misses, discovery_seconds, ticker_seconds,
                fx_seconds, ohlc_seconds, total_seconds,
            )
            return [results_by_symbol[pair["symbol"]] for pair in pairs if pair["symbol"] in results_by_symbol]

        # Compatibility path for small test doubles or explicitly configured watchlists.
        symbols = list(getattr(self.s, "watchlist", ()))
        stage_started = time.monotonic()
        tickers = self.exchange.tickers(symbols)
        ticker_seconds = time.monotonic() - stage_started
        self.market_snapshot_at = datetime.now(timezone.utc)
        self.market_pairs = [{"symbol": symbol, "quote": "EUR"} for symbol in symbols]
        self.market_ticker_count = len(tickers)
        results = []
        for symbol in symbols:
            stage_started = time.monotonic()
            results.append(self.scan_one(symbol, tickers))
            ohlc_seconds += time.monotonic() - stage_started
        total_seconds = time.monotonic() - scan_started
        self.last_scan_metrics = {
            "pairs": len(symbols),
            "tickers": len(tickers),
            "eligible": len(symbols),
            "ohlc_selected": len(symbols),
            "ohlc_budget": len(symbols),
            "ohlc_analyzed": sum(int(bool(item.get("ohlc_analyzed"))) for item in results),
            "duration_seconds": total_seconds,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        set_state(self.db, "last_scan_metrics", self.last_scan_metrics)
        log.info(
            "Market scan timing: pairs=%d tickers=%d eligible=%d OHLC_selected=%d "
            "OHLC_analyzed=%d discovery=%.2fs ticker=%.2fs fx=%.2fs "
            "OHLC=%.2fs total=%.2fs",
            len(symbols), len(tickers), len(symbols), len(symbols),
            sum(int(bool(item.get("ohlc_analyzed"))) for item in results),
            discovery_seconds, ticker_seconds, fx_seconds, ohlc_seconds, total_seconds,
        )
        return results



    def _execute_paper_fill(self, side, symbol, amount, price, prices, *, currency="EUR", quote_to_eur_rate=None):
        """Apply a simulated fill and atomically persist wallet plus trade history."""
        cash_before = dict(self.paper.cash_by_currency)
        cash_basis_before = dict(self.paper.cash_cost_basis_eur)
        positions_before = copy.deepcopy(self.paper.positions)
        try:
            if side == "Buy":
                fill = self.paper.buy(
                    symbol, amount, price, currency=currency,
                    quote_to_eur_rate=quote_to_eur_rate, persist=False,
                )
                fee_eur, realized_pnl_eur = self._paper_trade_eur_values(
                    currency, fill["fee"], prices,
                )
            elif side == "Sell":
                position_before = copy.deepcopy(self.paper.positions.get(symbol, {}))
                sell_currency = str(position_before.get("quote_currency", currency)).upper()
                sell_fx_rate = self._fresh_fx_rates(prices).get(sell_currency)
                fill = self.paper.sell(
                    symbol, amount, price, quote_to_eur_rate=sell_fx_rate, persist=False,
                )
                fee_eur, realized_pnl_eur = self._paper_trade_eur_values(
                    sell_currency, fill["fee"], prices,
                    position=position_before, qty=fill["qty"], exit_price=fill["price"],
                )
            else:
                raise ValueError(f"Unsupported PAPER fill side: {side}")
            save_paper_fill(
                self.db, cash_by_currency=self.paper.cash_by_currency,
                cash_cost_basis_eur=self.paper.cash_cost_basis_eur,
                positions=self.paper.positions, symbol=symbol, side=side,
                qty=fill["qty"], quote=fill["quote"], fee=fill["fee"],
                price=fill["price"], fee_eur=fee_eur,
                realized_pnl_eur=realized_pnl_eur,
            )
        except Exception:
            self.paper.cash_by_currency = cash_before
            self.paper.cash_cost_basis_eur = cash_basis_before
            self.paper.positions = positions_before
            self.paper._sync_cash()
            raise
        return fill, fee_eur, realized_pnl_eur

    def cycle(self):
        # Market monitoring continues while paused; only PAPER trading is gated.
        try:
            results = self.scan(seed_wallet=not self.paused)
            # Timestamp the market data itself, not the end of slower OHLC analysis.
            self.last_scan_at = self.market_snapshot_at or datetime.now(timezone.utc)
            self.last_prices = {x["symbol"]: float(x.get("price") or 0) for x in results if float(x.get("price") or 0) > 0}
            self.last_signals = results
            set_state(self.db, "paper_fx_rates", self._fx_rates(self.last_prices))
            if self.paused:
                # Keep the dashboard snapshot current while monitoring in PAUSED,
                # but never value non-EUR balances or positions with cached FX.
                self._save_cycle_snapshot(results, self.last_prices, require_fresh_fx=True)
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
        exit_blocked = False
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
                try:
                    depth_fill = self._paper_book_fill(symbol, pos["qty"], "sell")
                except ValueError as exc:
                    audit(self.db, "PAPER_EXIT_SKIPPED", f"{symbol}: {exc}")
                    actions.append(f"{symbol} HOLD — exit not simulated; visible depth insufficient or unavailable")
                    exit_blocked = True
                    continue
                if depth_fill is not None:
                    exit_price = depth_fill["vwap"]
                position_before_exit = dict(pos)
                # Profit-taking/strategy exits require positive net EUR P/L
                # after all costs. Protective exits (STOP_LOSS and TRAILING_STOP)
                # remain authoritative and may realize a loss; otherwise a
                # trailing stop could be neutralized by the profit-only guard.
                if reason not in {"STOP_LOSS", "TRAILING_STOP"}:
                    estimated_exit_fee = (
                        float(pos["qty"]) * exit_price * self.paper.fee_rate
                    )
                    _, estimated_net_pnl_eur = self._paper_trade_eur_values(
                        position_before_exit.get("quote_currency", "EUR"),
                        estimated_exit_fee, prices,
                        position=position_before_exit, qty=pos["qty"],
                        exit_price=exit_price,
                    )
                    if estimated_net_pnl_eur is None or estimated_net_pnl_eur <= 1e-9:
                        detail = (
                            f"{symbol} reason={reason} net_pnl_eur="
                            f"{estimated_net_pnl_eur!r}; waiting for net-positive exit"
                        )
                        audit(self.db, "PAPER_SELL_DEFERRED_NON_PROFIT", detail)
                        actions.append(f"{symbol} HOLD — sale deferred; net profit not positive")
                        continue
                fill, fee_eur, realized_pnl_eur = self._execute_paper_fill(
                    "Sell", symbol, pos["qty"], exit_price, prices,
                    currency=position_before_exit.get("quote_currency", "EUR"),
                )
                realized_pct = (exit_price / entry - 1) * 100
                audit(
                    self.db, reason,
                    f"{symbol} entry={entry:.10g} peak={peak:.10g} "
                    f"exit={exit_price:.10g} change={realized_pct:.3f}% "
                    f"net_pnl_eur={realized_pnl_eur!r}",
                )
                quote_currency = str(position_before_exit.get("quote_currency", "EUR")).upper()
                pnl_text = "n/a" if realized_pnl_eur is None else f"{realized_pnl_eur:+.4f}"
                actions.append(
                    f"{symbol} SELL {reason} | qty={fill['qty']:.8f} | quote={quote_currency} "
                    f"| entry={entry:.10g} | exit={fill['price']:.10g} "
                    f"| change={realized_pct:+.2f}% | fee_eur={fee_eur:.4f} | net_pnl_eur={pnl_text}"
                )
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
                try:
                    depth_fill = self._paper_book_fill(symbol, pos["qty"], "sell")
                except ValueError as exc:
                    reason = f"Daily loss limit reached; {symbol} cannot be fully simulated from visible depth: {exc}"
                    audit(self.db, "PAPER_DAILY_LIQUIDATION_INCOMPLETE", reason)
                    self._save_cycle_snapshot(results, prices)
                    self.stop(reason)
                    return actions + ["DAILY LOSS LIMIT — paused; position remains open because visible depth is insufficient"]
                if depth_fill is not None:
                    exit_price = depth_fill["vwap"]
                position_before_exit = dict(pos)
                fill, fee_eur, realized_pnl_eur = self._execute_paper_fill(
                    "Sell", symbol, pos["qty"], exit_price, prices,
                    currency=position_before_exit.get("quote_currency", "EUR"),
                )
                realized_pct = (exit_price / pos["avg_price"] - 1.0) * 100.0
                audit(
                    self.db, "DAILY_LOSS_LIQUIDATION",
                    f"{symbol} qty={fill['qty']:.10g} bid={exit_price:.10g} change={realized_pct:.3f}%",
                )
                quote_currency = str(position_before_exit.get("quote_currency", "EUR")).upper()
                pnl_text = "n/a" if realized_pnl_eur is None else f"{realized_pnl_eur:+.4f}"
                actions.append(
                    f"{symbol} SELL DAILY_LOSS_LIMIT | qty={fill['qty']:.8f} | quote={quote_currency} "
                    f"| entry={position_before_exit['avg_price']:.10g} | exit={fill['price']:.10g} "
                    f"| change={realized_pct:+.2f}% | fee_eur={fee_eur:.4f} | net_pnl_eur={pnl_text}"
                )
            self._save_cycle_snapshot(results, prices)
            self.stop("Daily loss limit reached; remaining PAPER positions liquidated")
            return actions + ["DAILY LOSS LIMIT — paused"]
        if exit_blocked:
            # Do not add exposure while a triggered exit could not be represented
            # by executable visible depth. Keep the existing position open and
            # retry its exit on the next market cycle.
            self._save_cycle_snapshot(results, prices)
            return actions
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
                # Never size risk using cached FX valuation fallbacks.
                equity_eur = self._equity_after_liquidation(prices, fresh_rates)
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
                        cash_before = dict(self.paper.cash_by_currency)
                        basis_before = dict(self.paper.cash_cost_basis_eur)
                        try:
                            if fee_legs == 2:
                                # Compute the second leg from the first leg's actual
                                # post-fee proceeds. Defer persistence until the
                                # wallet and aggregate fee ledger can commit atomically.
                                first_amount = source_amount
                                first_gross = first_amount * source_to_eur
                                first_received = first_gross * (1.0 - self.paper.fee_rate)
                                first_leg, second_leg = self.paper.convert_route([
                                    (source_currency, "EUR", source_amount, source_to_eur),
                                    ("EUR", quote_currency, first_received, quote_units_per_eur),
                                ], persist=False)
                                conversion = {
                                    "from": source_currency, "to": quote_currency,
                                    "spent": source_amount, "received": second_leg["received"],
                                    "fee": first_leg["fee"] + second_leg["fee"], "legs": 2,
                                }
                            elif source_currency == "EUR":
                                (conversion,) = self.paper.convert_route([
                                    ("EUR", quote_currency, source_amount, quote_units_per_eur),
                                ], persist=False)
                            else:
                                (conversion,) = self.paper.convert_route([
                                    (source_currency, "EUR", source_amount, source_to_eur),
                                ], persist=False)
                            if fee_legs == 2:
                                fx_fee_eur = first_leg["fee"] + second_leg["fee"] / quote_units_per_eur
                                fx_fee_amount = None
                                fx_fee_currency = "MULTI"
                            elif source_currency == "EUR":
                                fx_fee_eur = conversion["fee"] / quote_units_per_eur
                                fx_fee_amount = conversion["fee"]
                                fx_fee_currency = quote_currency
                            else:
                                fx_fee_eur = conversion["fee"]
                                fx_fee_amount = conversion["fee"]
                                fx_fee_currency = "EUR"
                            save_paper_fx_conversion(
                                self.db,
                                from_currency=conversion["from"], to_currency=conversion["to"],
                                spent=conversion["spent"], received=conversion["received"],
                                fee_amount=fx_fee_amount, fee_currency=fx_fee_currency,
                                fee_eur=fx_fee_eur, legs=fee_legs,
                                cash_by_currency=self.paper.cash_by_currency,
                                cash_cost_basis_eur=self.paper.cash_cost_basis_eur,
                                positions=self.paper.positions,
                            )
                        except Exception:
                            self.paper.cash_by_currency = cash_before
                            self.paper.cash_cost_basis_eur = basis_before
                            self.paper._sync_cash()
                            raise
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
                # Kraken PAPER buys consume visible asks. The quote budget is
                # capped by the worst requested visible level; no extra spread
                # or slippage penalty is layered on top of the book VWAP.
                ask_price = float(signal.get("ask") or signal["price"])
                depth_fill = self._paper_book_fill(
                    signal["symbol"], allocation / ask_price, "buy", max_quote=allocation,
                )
                if depth_fill is None:
                    entry_price = self._paper_buy_price(ask_price)
                    fill_quote = allocation
                else:
                    entry_price = depth_fill["vwap"]
                    fill_quote = depth_fill["quote"]
                    if fill_quote * (1.0 + self.paper.fee_rate) > quote_balance + 1e-9:
                        audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: depth VWAP exceeds available PAPER quote balance")
                        continue
                quote_to_eur_rate = self._fresh_fx_rates(prices).get(str(quote_currency).upper())
                fill, fee_eur, _ = self._execute_paper_fill(
                    "Buy", signal["symbol"], fill_quote, entry_price, prices,
                    currency=quote_currency, quote_to_eur_rate=quote_to_eur_rate,
                )
                actions.append(
                    f"{signal['symbol']} BUY | qty={fill['qty']:.8f} | quote={quote_currency} "
                    f"| price={fill['price']:.10g} | fee_eur={fee_eur:.4f}"
                )
            except (ValueError, PermissionError) as exc:
                audit(self.db, "ENTRY_SKIPPED", f"{signal['symbol']}: {exc}")
        self._save_cycle_snapshot(results, prices)
        return actions