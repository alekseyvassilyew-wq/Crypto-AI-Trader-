from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Limits:
    max_trade: float
    max_daily_loss: float
    max_positions: int
    max_spread_bps: float
    min_turnover: float


def _finite(name, value):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def check_order(quote_amount, spread_bps, turnover, open_positions, daily_pnl, limits):
    quote_amount = _finite("quote_amount", quote_amount)
    spread_bps = _finite("spread_bps", spread_bps)
    turnover = _finite("turnover", turnover)
    daily_pnl = _finite("daily_pnl", daily_pnl)
    max_trade = _finite("max_trade", limits.max_trade)
    max_daily_loss = _finite("max_daily_loss", limits.max_daily_loss)
    max_spread = _finite("max_spread_bps", limits.max_spread_bps)
    min_turnover = _finite("min_turnover", limits.min_turnover)

    if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
        raise ValueError("open_positions must be a non-negative integer")
    if isinstance(limits.max_positions, bool) or not isinstance(limits.max_positions, int) or limits.max_positions < 1:
        raise ValueError("max_positions must be a positive integer")
    if min(max_trade, max_daily_loss, max_spread) <= 0 or min_turnover < 0:
        raise ValueError("Risk limits must be positive and minimum turnover non-negative")
    if quote_amount <= 0 or quote_amount > max_trade:
        raise ValueError("Per-trade limit exceeded")
    if spread_bps < 0 or spread_bps > max_spread:
        raise ValueError("Spread filter failed")
    if turnover < min_turnover:
        raise ValueError("Liquidity filter failed")
    if open_positions >= limits.max_positions:
        raise ValueError("Maximum open positions reached")
    if daily_pnl <= -max_daily_loss:
        raise PermissionError("Daily loss limit reached")
