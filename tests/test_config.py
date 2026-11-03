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
    assert s.watchlist == ()\n    assert s.trade_quote_currency == "EUR"\n    assert s.max_candle_pairs == 10


def test_non_finite_risk_values_are_rejected(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("MAX_TRADE_EUR", "nan")
    with pytest.raises(ValueError, match="finite"):
        load_settings()


def test_non_eur_quote_currency_is_rejected_for_eur_paper_wallet(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("TRADE_QUOTE_CURRENCY", "USD")
    with pytest.raises(ValueError, match="denominated in EUR"):
        load_settings()
