import pytest
from src.trader.config import load_settings

def test_live_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    with pytest.raises(ValueError, match="PAPER-only"):
        load_settings()

def test_paper_config(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    s = load_settings()
    assert s.mode == "PAPER"
    assert s.paper_start == 20
    assert s.watchlist == ()
    assert s.trade_quote_currency == "EUR"
    assert s.max_candle_pairs == 20


def test_non_finite_risk_values_are_rejected(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("MAX_TRADE_EUR", "nan")
    with pytest.raises(ValueError, match="finite"):
        load_settings()


def test_non_eur_quote_currency_is_allowed_for_multi_currency_paper_wallet(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("TRADE_QUOTE_CURRENCY", "USD")
    settings = load_settings()
    assert settings.trade_quote_currency == "USD"
    assert settings.mode == "PAPER"


def test_mtf_and_conservative_risk_budget_are_enabled_by_default(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.delenv("ENABLE_MULTI_TIMEFRAME", raising=False)
    monkeypatch.delenv("RISK_PER_TRADE_PERCENT", raising=False)
    settings = load_settings()
    assert settings.enable_multi_timeframe is True
    assert settings.risk_per_trade_pct == 0.5
    assert settings.ml_auto_research_markets == 3


def test_excessive_per_trade_risk_budget_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("RISK_PER_TRADE_PERCENT", "3")
    with pytest.raises(ValueError, match="must not exceed"):
        load_settings()


def test_automatic_research_market_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("ML_AUTO_RESEARCH_MARKETS", "6")
    with pytest.raises(ValueError, match="must not exceed 5"):
        load_settings()
