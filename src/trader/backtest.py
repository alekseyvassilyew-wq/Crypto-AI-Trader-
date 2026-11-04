"""Fee-aware, no-lookahead historical PAPER backtesting for one Kraken Spot market.

Signals are computed only from candles closed before the simulated order. Orders
fill at the following candle's open with configurable adverse slippage.
"""
import math

from .indicators import analyze


def _number(value, name, *, minimum=0.0, strict=False):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or (result <= minimum if strict else result < minimum):
        op = "greater than" if strict else "at least"
        raise ValueError(f"{name} must be {op} {minimum}")
    return result


def run_backtest(
    candles,
    *,
    initial_cash=1000.0,
    max_trade=100.0,
    fee_rate=0.0026,
    slippage_bps=5.0,
    stop_loss_pct=2.0,
    take_profit_pct=4.0,
    trailing_stop_pct=1.5,
    signal_fn=None,
):
    """Run a single-market long-only spot simulation on normalized OHLC rows.

    Rows are [timestamp, open, high, low, close, volume], oldest first.
    If stop and target are both touched within one candle, the stop is assumed
    to execute first (conservative intrabar ordering).
    """
    cash0 = _number(initial_cash, "initial_cash", strict=True)
    budget = _number(max_trade, "max_trade", strict=True)
    fee = _number(fee_rate, "fee_rate")
    if fee >= 0.1:
        raise ValueError("fee_rate must be below 0.1")
    slip = _number(slippage_bps, "slippage_bps") / 10000.0
    if slip >= 0.1:
        raise ValueError("slippage_bps must be below 1000")
    stop_pct = _number(stop_loss_pct, "stop_loss_pct", strict=True) / 100.0
    target_pct = _number(take_profit_pct, "take_profit_pct", strict=True) / 100.0
    trail_pct = _number(trailing_stop_pct, "trailing_stop_pct", strict=True) / 100.0
    if not isinstance(candles, (list, tuple)) or len(candles) < 62:
        raise ValueError("At least 62 closed candles are required")
    rows = []
    previous_ts = None
    for index, row in enumerate(candles):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError(f"Invalid candle at index {index}")
        ts = _number(row[0], "timestamp")
        op = _number(row[1], "open", strict=True)
        high = _number(row[2], "high", strict=True)
        low = _number(row[3], "low", strict=True)
        close = _number(row[4], "close", strict=True)
        volume = _number(row[5], "volume")
        if low > min(op, close) or high < max(op, close) or low > high:
            raise ValueError(f"Inconsistent OHLC values at index {index}")
        if previous_ts is not None and ts <= previous_ts:
            raise ValueError("Candles must be strictly chronological")
        previous_ts = ts
        rows.append((ts, op, high, low, close, volume))

    signal_fn = signal_fn or analyze
    cash = cash0
    qty = 0.0
    entry = 0.0
    peak = 0.0
    entry_fee = 0.0
    entry_time = None
    trades = []
    equity_curve = [cash0]
    total_fees = 0.0
    wins = []
    losses = []
    max_equity = cash0
    max_drawdown = 0.0

    def close_position(price, timestamp, reason):
        nonlocal cash, qty, entry, peak, entry_fee, entry_time, total_fees
        fill = price * (1.0 - slip)
        gross = qty * fill
        exit_fee = gross * fee
        cash += gross - exit_fee
        total_fees += exit_fee
        net_pnl = gross - exit_fee - (qty * entry + entry_fee)
        trade = {
            "entry_time": entry_time,
            "exit_time": timestamp,
            "entry_price": entry,
            "exit_price": fill,
            "quantity": qty,
            "net_pnl": net_pnl,
            "return_pct": net_pnl / (qty * entry + entry_fee) * 100.0,
            "reason": reason,
            "fees": entry_fee + exit_fee,
        }
        trades.append(trade)
        (wins if net_pnl > 0 else losses).append(net_pnl)
        qty = entry = peak = entry_fee = 0.0
        entry_time = None

    for i in range(60, len(rows) - 1):
        current = rows[i]
        next_candle = rows[i + 1]
        ts, op, high, low, close, volume = current
        next_ts, next_open = next_candle[0], next_candle[1]

        # Exit decisions use only a peak recorded before this candle.
        if qty:
            stop_price = entry * (1.0 - stop_pct)
            trail_price = peak * (1.0 - trail_pct) if peak > entry else 0.0
            active_stop = max(stop_price, trail_price)
            target_price = entry * (1.0 + target_pct)
            exit_reason = None
            exit_at = None
            if op <= active_stop:
                exit_reason, exit_at = "STOP_LOSS" if active_stop == stop_price else "TRAILING_STOP", op
            elif op >= target_price:
                exit_reason, exit_at = "TAKE_PROFIT", op
            elif low <= active_stop:
                exit_reason = "STOP_LOSS" if active_stop == stop_price else "TRAILING_STOP"
                exit_at = active_stop
            elif high >= target_price:
                exit_reason, exit_at = "TAKE_PROFIT", target_price
            if exit_reason:
                close_position(exit_at, ts, exit_reason)
            else:
                peak = max(peak, high)

        # The signal is based on data through this candle's close; fill next open.
        if not qty and cash > 0:
            signal = signal_fn([list(x) for x in rows[: i + 1]])
            if signal.get("signal") == "BUY":
                spend = min(budget, cash / (1.0 + fee))
                fill = next_open * (1.0 + slip)
                entry_fee = spend * fee
                qty = (spend - entry_fee) / fill
                cash -= spend
                total_fees += entry_fee
                entry = fill
                peak = fill
                entry_time = next_ts

        mark = next_open
        equity = cash + (qty * mark if qty else 0.0)
        equity_curve.append(equity)
        max_equity = max(max_equity, equity)
        max_drawdown = max(max_drawdown, (max_equity - equity) / max_equity if max_equity else 0.0)

    if qty:
        last = rows[-1]
        close_position(last[4], last[0], "END_OF_DATA")
        equity_curve.append(cash)
        max_equity = max(max_equity, cash)
        max_drawdown = max(max_drawdown, (max_equity - cash) / max_equity if max_equity else 0.0)

    net_pnl = cash - cash0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "initial_cash": cash0,
        "final_equity": cash,
        "net_pnl": net_pnl,
        "return_pct": net_pnl / cash0 * 100.0,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0),
        "max_drawdown_pct": max_drawdown * 100.0,
        "fees_paid": total_fees,
        "trades_detail": trades,
        "candles": len(rows),
        "assumptions": {
            "fee_rate_pct_per_side": fee * 100.0,
            "slippage_bps_per_side": slippage_bps,
            "execution": "next candle open",
            "intrabar_priority": "stop before take-profit",
        },
    }
