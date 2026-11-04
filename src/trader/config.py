from dataclasses import dataclass
import math
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    mode: str
    db_path: str
    paper_start: float
    max_trade: float
    max_daily_loss: float
    risk_per_trade_pct: float
    max_positions: int
    max_spread_bps: float
    min_turnover: float
    fee_rate: float
    watchlist: tuple[str, ...]
    trade_quote_currency: str
    max_candle_pairs: int
    enable_multi_timeframe: bool
    ml_auto_research_markets: int
    scan_interval: int
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    kraken_api_key: str
    kraken_api_secret: str
    telegram_group_chat_id: int
    topic_general_id: int
    topic_trades_id: int
    topic_portfolio_id: int
    topic_profit_id: int
    topic_errors_id: int


def load_settings():
    mode = os.getenv("TRADING_MODE", "PAPER").strip().upper()
    if mode != "PAPER":
        raise ValueError("This release is PAPER-only. Live order placement is intentionally disabled.")
    s = Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        admin_id=int(os.getenv("ADMIN_ID", "0")),
        mode=mode,
        db_path=os.getenv("DATABASE_PATH", "/var/data/crypto_ai_trader.sqlite3"),
        paper_start=float(os.getenv("PAPER_START_EUR", "20")),
        max_trade=float(os.getenv("MAX_TRADE_EUR", "5")),
        max_daily_loss=float(os.getenv("MAX_DAILY_LOSS_EUR", "2")),
        risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PERCENT", "0.5")),
        max_positions=int(os.getenv("MAX_OPEN_POSITIONS", "2")),
        max_spread_bps=float(os.getenv("MAX_SPREAD_BPS", "25")),
        min_turnover=float(os.getenv("MIN_24H_TURNOVER_EUR", "10000")),
        fee_rate=float(os.getenv("PAPER_FEE_RATE", "0.0026")),
        watchlist=tuple(x.strip().upper().replace("/", "") for x in os.getenv("WATCHLIST", "").split(",") if x.strip()),
        trade_quote_currency=os.getenv("TRADE_QUOTE_CURRENCY", "EUR").strip().upper(),
        max_candle_pairs=int(os.getenv("MAX_CANDLE_PAIRS", "20")),
        enable_multi_timeframe=os.getenv("ENABLE_MULTI_TIMEFRAME", "true").strip().lower() in ("1", "true", "yes", "on"),
        ml_auto_research_markets=int(os.getenv("ML_AUTO_RESEARCH_MARKETS", "3")),
        scan_interval=int(os.getenv("SCAN_INTERVAL_SECONDS", "60")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PERCENT", "2")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PERCENT", "4")),
        trailing_stop_pct=float(os.getenv("TRAILING_STOP_PERCENT", "1.5")),
        kraken_api_key=os.getenv("KRAKEN_API_KEY", "").strip(),
        kraken_api_secret=os.getenv("KRAKEN_API_SECRET", "").strip(),
        telegram_group_chat_id=int(os.getenv("TELEGRAM_GROUP_CHAT_ID", "0")),
        topic_general_id=int(os.getenv("TOPIC_GENERAL_ID", "0")),
        topic_trades_id=int(os.getenv("TOPIC_TRADES_ID", "0")),
        topic_portfolio_id=int(os.getenv("TOPIC_PORTFOLIO_ID", "0")),
        topic_profit_id=int(os.getenv("TOPIC_PROFIT_ID", "0")),
        topic_errors_id=int(os.getenv("TOPIC_ERRORS_ID", "0")),
    )
    if not s.bot_token or s.admin_id <= 0:
        raise ValueError("Set BOT_TOKEN and numeric ADMIN_ID.")
    if s.paper_start <= 0 or s.max_trade <= 0 or s.max_trade > s.paper_start:
        raise ValueError("Invalid paper balance or trade size configuration.")
    numeric_values = (s.paper_start, s.max_trade, s.max_daily_loss, s.risk_per_trade_pct, s.max_spread_bps,
                      s.min_turnover, s.fee_rate, s.stop_loss_pct, s.take_profit_pct, s.trailing_stop_pct)
    if not all(math.isfinite(value) for value in numeric_values):
        raise ValueError("All numeric settings must be finite numbers.")
    if min(s.max_daily_loss, s.max_positions, s.max_candle_pairs, s.ml_auto_research_markets, s.scan_interval, s.stop_loss_pct, s.take_profit_pct, s.trailing_stop_pct, s.risk_per_trade_pct) <= 0:
        raise ValueError("Risk limits and scan interval must be positive.")
    # TRADE_QUOTE_CURRENCY is retained for backwards-compatible display/defaults.
    if s.max_spread_bps <= 0 or s.min_turnover < 0:
        raise ValueError("Spread limit must be positive and minimum turnover cannot be negative.")
    if s.ml_auto_research_markets > 5:
        raise ValueError("ML_AUTO_RESEARCH_MARKETS must not exceed 5.")
    if s.risk_per_trade_pct > 2.0:
        raise ValueError("RISK_PER_TRADE_PERCENT must not exceed 2.0%.")
    if not 0 <= s.fee_rate < 0.1:
        raise ValueError("PAPER_FEE_RATE must be between 0 and 0.1.")
    return s
