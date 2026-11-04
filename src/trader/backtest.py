"""Conservative, fee-aware walk-forward backtest for the current indicator strategy."""
from .indicators import analyze


def run_backtest(rows, fee_rate=0.0026, slippage_bps=5.0, stop_loss_pct=2.0, take_profit_pct=4.0,
                 initial_cash=1000.0):
    """Replay closed candles without look-ahead; enter at next candle open.

    If both stop and target are touched inside one candle, assume the stop is hit
    first. This is deliberately conservative. Results are research only.
    """
    if not 0 <= fee_rate < 0.1:
        raise ValueError("fee_rate must be between 0 and 0.1")
    if slippage_bps < 0 or initial_cash <= 0 or stop_loss_pct <= 0 or take_profit_pct <= 0:
        raise ValueError("Invalid backtest parameters")
    candles = list(rows)
    if len(candles) < 61:
        raise ValueError("At least 61 closed candles are required")
    slip = slippage_bps / 10000.0
    cash = float(initial_cash)
    position = None
    trades = []
    equity_curve = [cash]

    def close_position(raw_price, reason):
        nonlocal cash, position
        exit_price = raw_price * (1 - slip)
        proceeds = position["qty"] * exit_price
        exit_fee = proceeds * fee_rate
        cash = proceeds - exit_fee
        pnl = cash - position["capital_before"]
        trades.append({
            "entry": position["entry_price"], "exit": exit_price,
            "reason": reason, "pnl": pnl,
            "return_pct": pnl / position["capital_before"] * 100,
            "fees": position["entry_fee"] + exit_fee,
        })
        position = None

    for index in range(60, len(candles)):
        candle = candles[index]
        open_price, high, low, close = map(float, (candle[1], candle[2], candle[3], candle[4]))
        exited = False
        opened = False

        if position is not None:
            stop = position["entry_price"] * (1 - stop_loss_pct / 100)
            target = position["entry_price"] * (1 + take_profit_pct / 100)
            # Conservative intrabar assumption: stop wins if both levels touched.
            if low <= stop:
                close_position(stop, "STOP_LOSS")
                exited = True
            elif high >= target:
                close_position(target, "TAKE_PROFIT")
                exited = True

        if position is None and not exited:
            signal = analyze(candles[:index])
            if signal.get("signal") == "BUY" and cash > 0:
                entry_price = open_price * (1 + slip)
                capital_before = cash
                entry_fee = capital_before * fee_rate
                qty = (capital_before - entry_fee) / entry_price
                position = {
                    "qty": qty, "entry_price": entry_price,
                    "capital_before": capital_before, "entry_fee": entry_fee,
                }
                cash = 0.0
                opened = True

        # Check the entry candle after the simulated open fill.
        if opened and position is not None:
            stop = position["entry_price"] * (1 - stop_loss_pct / 100)
            target = position["entry_price"] * (1 + take_profit_pct / 100)
            if low <= stop:
                close_position(stop, "STOP_LOSS")
            elif high >= target:
                close_position(target, "TAKE_PROFIT")

        marked_equity = cash
        if position is not None:
            marked_equity = position["qty"] * close * (1 - slip) * (1 - fee_rate)
        equity_curve.append(marked_equity)

    if position is not None:
        close_position(float(candles[-1][4]), "END_OF_DATA")
        equity_curve.append(cash)

    peak = initial_cash
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)

    wins = sum(1 for trade in trades if trade["pnl"] > 0)
    total_return = (cash / initial_cash - 1) * 100
    first_open = float(candles[0][1])
    last_close = float(candles[-1][4])
    benchmark = ((last_close * (1 - slip) * (1 - fee_rate)) /
                 (first_open * (1 + slip) * (1 + fee_rate)) - 1) * 100

    return {
        "initial_cash": initial_cash,
        "final_equity": cash,
        "return_pct": total_return,
        "buy_hold_pct": benchmark,
        "max_drawdown_pct": max_drawdown,
        "trades": len(trades),
        "wins": wins,
        "win_rate_pct": (wins / len(trades) * 100) if trades else 0.0,
        "fees_total": sum(trade["fees"] for trade in trades),
        "trade_details": trades,
        "candles": len(candles),
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
    }
