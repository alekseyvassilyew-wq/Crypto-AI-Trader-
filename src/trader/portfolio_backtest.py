"""Shared-cash, multi-market PAPER backtesting for Kraken Spot.

All markets must have aligned, chronological OHLCV candles. Signals use only
closed candles and fills occur at the next candle open. Quote-currency balances
are shared across markets and valued in EUR using supplied EUR-per-unit FX rates.
"""
import math

from .indicators import analyze


def _positive(value, name, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric, not boolean")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError(f"{name} must be finite and {'non-negative' if allow_zero else 'positive'}")
    return number


def run_portfolio_backtest(
    candles_by_symbol,
    *,
    quote_currencies,
    fx_rates,
    fx_rates_by_timestamp=None,
    initial_cash_eur=1000.0,
    max_trade_eur=100.0,
    max_positions=5,
    risk_per_trade_pct=0.5,
    fee_rate=0.0026,
    slippage_bps=5.0,
    stop_loss_pct=2.0,
    take_profit_pct=4.0,
    trailing_stop_pct=1.5,
    max_daily_loss_eur=None,
    signal_fn=None,
):
    """Simulate several spot markets against one shared multi-currency wallet.

    fx_rates maps quote currencies to EUR per unit (EUR must be 1.0).
    Optional fx_rates_by_timestamp maps candle timestamps to dated EUR-per-unit
    rates. Missing currencies fall back to fx_rates. Entry and exit valuations
    use the rates at their respective fill timestamps.
    signal_fn is called as signal_fn(symbol, history); default is indicators.analyze.
    """
    if not isinstance(candles_by_symbol, dict) or not candles_by_symbol:
        raise ValueError("candles_by_symbol must be a non-empty mapping")
    if not isinstance(quote_currencies, dict) or not isinstance(fx_rates, dict):
        raise ValueError("quote_currencies and fx_rates must be mappings")
    initial = _positive(initial_cash_eur, "initial_cash_eur")
    trade_cap = _positive(max_trade_eur, "max_trade_eur")
    if trade_cap > initial:
        raise ValueError("max_trade_eur cannot exceed initial_cash_eur")
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or max_positions < 1 or max_positions > 10:
        raise ValueError("max_positions must be an integer between 1 and 10")
    risk_pct = _positive(risk_per_trade_pct, "risk_per_trade_pct")
    if risk_pct > 2.0:
        raise ValueError("risk_per_trade_pct must not exceed 2.0%")
    fee = _positive(fee_rate, "fee_rate", allow_zero=True)
    slip_bps = _positive(slippage_bps, "slippage_bps", allow_zero=True)
    if fee >= 0.1 or slip_bps >= 1000:
        raise ValueError("fee_rate must be below 0.1 and slippage_bps below 1000")
    stop = _positive(stop_loss_pct, "stop_loss_pct") / 100
    target = _positive(take_profit_pct, "take_profit_pct") / 100
    trail = _positive(trailing_stop_pct, "trailing_stop_pct") / 100
    daily_limit = None if max_daily_loss_eur is None else _positive(max_daily_loss_eur, "max_daily_loss_eur")
    if daily_limit is not None and daily_limit > initial:
        raise ValueError("max_daily_loss_eur cannot exceed initial_cash_eur")
    rates = {str(k).upper(): _positive(v, f"fx_rates[{k}]") for k, v in fx_rates.items()}
    rates.setdefault("EUR", 1.0)
    if not math.isclose(rates["EUR"], 1.0, rel_tol=0, abs_tol=1e-12):
        raise ValueError("EUR FX rate must equal 1.0")
    dated_rates = {}
    if fx_rates_by_timestamp is not None:
        if not isinstance(fx_rates_by_timestamp, dict):
            raise ValueError("fx_rates_by_timestamp must be a mapping")
        for raw_timestamp, raw_rates in fx_rates_by_timestamp.items():
            timestamp = _positive(raw_timestamp, "FX timestamp", allow_zero=True)
            if not isinstance(raw_rates, dict):
                raise ValueError("Each dated FX rate entry must be a mapping")
            parsed = {str(k).upper(): _positive(v, f"dated FX rate {k}") for k, v in raw_rates.items()}
            if "EUR" in parsed and not math.isclose(parsed["EUR"], 1.0, rel_tol=0, abs_tol=1e-12):
                raise ValueError("EUR FX rate must equal 1.0")
            parsed["EUR"] = 1.0
            dated_rates[timestamp] = parsed

    def rates_at(timestamp):
        current = dict(rates)
        current.update(dated_rates.get(float(timestamp), {}))
        return current

    markets = {}
    common_timestamps = None
    for raw_symbol, raw_rows in candles_by_symbol.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in markets:
            raise ValueError("Market symbols must be unique non-empty strings")
        quote = str(quote_currencies.get(raw_symbol, quote_currencies.get(symbol, ""))).strip().upper()
        if not quote or quote not in rates:
            raise ValueError(f"Missing quote currency or EUR FX rate for {symbol}")
        if not isinstance(raw_rows, (list, tuple)) or len(raw_rows) < 62:
            raise ValueError(f"At least 62 candles are required for {symbol}")
        rows = []
        previous = None
        for i, row in enumerate(raw_rows):
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise ValueError(f"Invalid candle for {symbol} at index {i}")
            ts = _positive(row[0], f"{symbol} timestamp", allow_zero=True)
            op, high, low, close = (_positive(row[j], f"{symbol} OHLC") for j in (1, 2, 3, 4))
            volume = _positive(row[5], f"{symbol} volume", allow_zero=True)
            if low > min(op, close) or high < max(op, close) or low > high:
                raise ValueError(f"Inconsistent OHLC values for {symbol} at index {i}")
            if previous is not None and ts <= previous:
                raise ValueError(f"Candles for {symbol} must be strictly chronological")
            previous = ts
            rows.append((ts, op, high, low, close, volume))
        timestamps = [r[0] for r in rows]
        if common_timestamps is None:
            common_timestamps = timestamps
        elif timestamps != common_timestamps:
            raise ValueError("All markets must have identical candle timestamps")
        markets[symbol] = {"rows": rows, "quote": quote}

    signal_fn = signal_fn or (lambda symbol, history: analyze(history))
    cash = {"EUR": initial}
    positions = {}
    trades = []
    fees_paid = 0.0
    equity_curve = []
    max_equity = initial
    max_drawdown = 0.0
    daily_start = initial
    active_day = None
    trading_halted = False
    slippage = slip_bps / 10000.0

    def equity(marks, timestamp):
        current_rates = rates_at(timestamp)
        total = sum(amount * current_rates.get(currency, 0.0) for currency, amount in cash.items())
        for symbol, pos in positions.items():
            price = marks.get(symbol, pos["entry_price"])
            total += pos["qty"] * price * current_rates[pos["quote"]]
        return total

    def close(symbol, price, timestamp, reason):
        nonlocal fees_paid
        pos = positions.pop(symbol)
        current_rates = rates_at(timestamp)
        fill = price * (1 - slippage)
        gross = pos["qty"] * fill
        exit_fee = gross * fee
        cash[pos["quote"]] = cash.get(pos["quote"], 0.0) + gross - exit_fee
        fees_paid += exit_fee * current_rates[pos["quote"]]
        conversion_fee_eur = float(pos.get("conversion_fee_eur", 0.0))
        entry_cost_eur = (pos["entry_quote"] + pos["entry_fee"]) * pos["entry_fx_rate"] + conversion_fee_eur
        net_pnl_eur = ((gross - exit_fee) * current_rates[pos["quote"]]) - entry_cost_eur
        trades.append({
            "symbol": symbol, "quote_currency": pos["quote"],
            "entry_time": pos["entry_time"], "exit_time": timestamp,
            "entry_price": pos["entry_price"], "exit_price": fill,
            "quantity": pos["qty"], "net_pnl_eur": net_pnl_eur,
            "return_pct": net_pnl_eur / entry_cost_eur * 100 if entry_cost_eur else 0.0,
            "conversion_fee_eur": conversion_fee_eur,
            "entry_fee_eur": pos["entry_fee"] * pos["entry_fx_rate"],
            "exit_fee_eur": exit_fee * current_rates[pos["quote"]],
            "fees_eur": conversion_fee_eur + pos["entry_fee"] * pos["entry_fx_rate"] + exit_fee * current_rates[pos["quote"]],
            "reason": reason,
        })

    for index in range(60, len(common_timestamps) - 1):
        timestamp = common_timestamps[index]
        day = int(timestamp // 86400)
        if active_day != day:
            active_day = day
            daily_start = equity({s: m["rows"][index][4] for s, m in markets.items()}, timestamp)
            trading_halted = False
        marks = {s: m["rows"][index][4] for s, m in markets.items()}

        # Manage all existing positions before considering new entries.
        for symbol in list(positions):
            row = markets[symbol]["rows"][index]
            _, op, high, low, close_price, _ = row
            pos = positions[symbol]
            stop_price = pos["entry_price"] * (1 - stop)
            trailing_price = pos["peak"] * (1 - trail) if pos["peak"] > pos["entry_price"] else 0.0
            active_stop = max(stop_price, trailing_price)
            target_price = pos["entry_price"] * (1 + target)
            reason = None
            exit_price = None
            if op <= active_stop:
                reason, exit_price = ("STOP_LOSS" if active_stop == stop_price else "TRAILING_STOP"), op
            elif op >= target_price:
                reason, exit_price = "TAKE_PROFIT", op
            elif low <= active_stop:
                reason, exit_price = ("STOP_LOSS" if active_stop == stop_price else "TRAILING_STOP"), active_stop
            elif high >= target_price:
                reason, exit_price = "TAKE_PROFIT", target_price
            if reason:
                close(symbol, exit_price, timestamp, reason)
            else:
                pos["peak"] = max(pos["peak"], high)

        # Mark remaining positions at both candle extremes. Using the
        # high before the low is a conservative worst-case drawdown assumption.
        high_marks = {symbol: markets[symbol]["rows"][index][2] for symbol in positions}
        low_marks = {symbol: markets[symbol]["rows"][index][3] for symbol in positions}
        intrabar_high_equity = equity(high_marks, timestamp)
        intrabar_low_equity = equity(low_marks, timestamp)
        max_equity = max(max_equity, intrabar_high_equity)
        if max_equity > 0:
            max_drawdown = max(max_drawdown, (max_equity - intrabar_low_equity) / max_equity)
        current_equity = equity(marks, timestamp)
        if daily_limit is not None and intrabar_low_equity - daily_start <= -daily_limit:
            trading_halted = True

        # Signals are based on this closed candle; any order fills at next open.
        candidates = []
        if not trading_halted:
            for symbol, market in markets.items():
                if symbol in positions:
                    continue
                history = [list(r) for r in market["rows"][:index + 1]]
                signal = signal_fn(symbol, history)
                if isinstance(signal, dict) and signal.get("signal") == "BUY":
                    score = float(signal.get("score") or 0)
                    if not math.isfinite(score):
                        continue
                    candidates.append((score, symbol, signal))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

        for _, symbol, signal in candidates:
            if len(positions) >= int(max_positions):
                break
            market = markets[symbol]
            quote = market["quote"]
            next_timestamp = common_timestamps[index + 1]
            execution_rates = rates_at(next_timestamp)
            quote_rate = execution_rates[quote]
            next_open = market["rows"][index + 1][1]
            next_marks = {s: m["rows"][index + 1][1] for s, m in markets.items()}
            current_equity = equity(next_marks, next_timestamp)
            available_eur = sum(amount * execution_rates.get(currency, 0.0) for currency, amount in cash.items())
            atr_percent = float(signal.get("atr_percent") or 0.0)
            if not math.isfinite(atr_percent) or atr_percent < 0:
                continue
            stop_distance_pct = max(stop * 100.0, atr_percent * 1.5, 0.5)
            risk_budget_eur = current_equity * risk_pct / 100.0
            budget_eur = min(trade_cap, available_eur, risk_budget_eur / (stop_distance_pct / 100.0))
            if budget_eur <= 0:
                break
            required_quote = budget_eur / quote_rate
            quote_balance = cash.get(quote, 0.0)
            conversion_fee_for_trade = 0.0
            if quote_balance + 1e-12 < required_quote * (1 + fee):
                # Convert only the shortfall from EUR; never borrow or rebalance positions.
                shortfall_quote = required_quote * (1 + fee) - quote_balance
                source_eur = shortfall_quote * quote_rate / (1 - fee)
                if cash.get("EUR", 0.0) + 1e-12 < source_eur:
                    # Use any other funded quote wallet via its EUR valuation.
                    for source in sorted(c for c, amount in cash.items() if c != quote and amount > 1e-12):
                        source_rate = execution_rates.get(source)
                        if not source_rate:
                            continue
                        source_amount = min(cash[source], source_eur / source_rate)
                        received = source_amount * source_rate / quote_rate * (1 - fee)
                        cash[source] -= source_amount
                        cash[quote] = cash.get(quote, 0.0) + received
                        conversion_fee = source_amount * source_rate * fee
                        fees_paid += conversion_fee
                        conversion_fee_for_trade += conversion_fee
                        quote_balance = cash[quote]
                        if quote_balance + 1e-12 >= required_quote * (1 + fee):
                            break
                else:
                    cash["EUR"] -= source_eur
                    received = source_eur / quote_rate * (1 - fee)
                    cash[quote] = quote_balance + received
                    conversion_fee = source_eur * fee
                    fees_paid += conversion_fee
                    conversion_fee_for_trade += conversion_fee
            quote_balance = cash.get(quote, 0.0)
            spend = min(required_quote, quote_balance / (1 + fee))
            if spend <= 0:
                continue
            fill = next_open * (1 + slippage)
            entry_fee = spend * fee
            qty = spend / fill
            cash[quote] -= spend + entry_fee
            fees_paid += entry_fee
            positions[symbol] = {
                "quote": quote, "qty": qty, "entry_price": fill,
                "entry_quote": spend, "entry_fee": entry_fee,
                "entry_time": common_timestamps[index + 1], "peak": fill,
                "entry_fx_rate": rates_at(common_timestamps[index + 1])[quote],
                "conversion_fee_eur": conversion_fee_for_trade,
            }

        next_marks = {s: m["rows"][index + 1][1] for s, m in markets.items()}
        current_equity = equity(next_marks, common_timestamps[index + 1])
        equity_curve.append({"timestamp": common_timestamps[index + 1], "equity_eur": current_equity})
        max_equity = max(max_equity, current_equity)
        max_drawdown = max(max_drawdown, (max_equity - current_equity) / max_equity if max_equity else 0.0)

    # Apply stop/target logic to the final candle before forced liquidation.
    last_timestamp = common_timestamps[-1]
    for symbol in list(positions):
        _, op, high, low, close_price, _ = markets[symbol]["rows"][-1]
        pos = positions[symbol]
        stop_price = pos["entry_price"] * (1 - stop)
        trailing_price = pos["peak"] * (1 - trail) if pos["peak"] > pos["entry_price"] else 0.0
        active_stop = max(stop_price, trailing_price)
        target_price = pos["entry_price"] * (1 + target)
        if op <= active_stop:
            reason, exit_price = ("STOP_LOSS" if active_stop == stop_price else "TRAILING_STOP"), op
        elif op >= target_price:
            reason, exit_price = "TAKE_PROFIT", op
        elif low <= active_stop:
            reason, exit_price = ("STOP_LOSS" if active_stop == stop_price else "TRAILING_STOP"), active_stop
        elif high >= target_price:
            reason, exit_price = "TAKE_PROFIT", target_price
        else:
            reason, exit_price = "END_OF_DATA", close_price
        close(symbol, exit_price, last_timestamp, reason)
    final_rates = rates_at(last_timestamp)
    final_equity = sum(amount * final_rates.get(currency, 0.0) for currency, amount in cash.items())
    # Include final forced liquidation costs in both the equity curve and drawdown.
    equity_curve.append({"timestamp": last_timestamp, "equity_eur": final_equity})
    max_equity = max(max_equity, final_equity)
    max_drawdown = max(
        max_drawdown,
        (max_equity - final_equity) / max_equity if max_equity else 0.0,
    )
    net_pnl = final_equity - initial

    # Equal-weight buy-and-hold benchmark on the same markets and executable
    # window, including conversion, entry/exit fees, and adverse slippage.
    benchmark_entry_timestamp = trades[0]["entry_time"] if trades else common_timestamps[61]
    benchmark_entry_index = next(
        (index for index, timestamp in enumerate(common_timestamps) if timestamp >= benchmark_entry_timestamp),
        61,
    )
    benchmark_exit_timestamp = common_timestamps[-1]
    benchmark_entry_rates = rates_at(common_timestamps[benchmark_entry_index])
    benchmark_exit_rates = rates_at(benchmark_exit_timestamp)
    benchmark_allocation_eur = initial / len(markets)
    benchmark_final_equity = 0.0
    for symbol, market in markets.items():
        quote = market["quote"]
        quote_rate = benchmark_entry_rates[quote]
        quote_allocation = benchmark_allocation_eur / quote_rate
        if quote != "EUR":
            quote_allocation *= 1.0 - fee
        spend = quote_allocation / (1.0 + fee)
        entry_fill = market["rows"][benchmark_entry_index][1] * (1.0 + slippage)
        quantity = spend / entry_fill
        exit_gross = quantity * market["rows"][-1][4] * (1.0 - slippage)
        exit_net = exit_gross * (1.0 - fee)
        benchmark_final_equity += exit_net * benchmark_exit_rates[quote]
    benchmark_return_pct = (benchmark_final_equity / initial - 1.0) * 100.0

    wins = [t for t in trades if t["net_pnl_eur"] > 0]
    losses = [t for t in trades if t["net_pnl_eur"] <= 0]
    gross_profit = sum(t["net_pnl_eur"] for t in wins)
    gross_loss = abs(sum(t["net_pnl_eur"] for t in losses))
    return {
        "initial_equity_eur": initial, "final_equity_eur": final_equity,
        "net_pnl_eur": net_pnl, "return_pct": net_pnl / initial * 100,
        "buy_hold_final_equity_eur": benchmark_final_equity,
        "buy_hold_return_pct": benchmark_return_pct,
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0),
        "max_drawdown_pct": max_drawdown * 100, "fees_paid_eur": fees_paid,
        "cash_by_currency": cash, "trades_detail": trades, "equity_curve": equity_curve,
        "markets": list(markets), "assumptions": {
            "fee_rate_pct_per_side": fee * 100,
            "slippage_bps_per_side": slip_bps,
            "execution": "next candle open",
            "intrabar_priority": "stop before take-profit",
            "buy_hold": "equal-weight same markets from first strategy entry (or first executable candle if no trades) through final close; includes conversion and two-sided fees/slippage",
            "fx_rates_fallback": rates,
            "dated_fx_rate_timestamps": len(dated_rates),
        },
    }
