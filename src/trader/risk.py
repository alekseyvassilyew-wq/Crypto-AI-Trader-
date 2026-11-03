from dataclasses import dataclass

@dataclass(frozen=True)
class Limits:
    max_trade: float
    max_daily_loss: float
    max_positions: int
    max_spread_bps: float
    min_turnover: float

def check_order(quote_amount, spread_bps, turnover, open_positions, daily_pnl, limits):
    if quote_amount <= 0 or quote_amount > limits.max_trade: raise ValueError("Per-trade limit exceeded")
    if spread_bps < 0 or spread_bps > limits.max_spread_bps: raise ValueError("Spread filter failed")
    if turnover < limits.min_turnover: raise ValueError("Liquidity filter failed")
    if open_positions >= limits.max_positions: raise ValueError("Maximum open positions reached")
    if daily_pnl <= -limits.max_daily_loss: raise PermissionError("Daily loss limit reached")
