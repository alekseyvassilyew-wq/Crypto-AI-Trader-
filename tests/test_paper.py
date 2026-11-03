import pytest
from src.trader.paper import PaperBroker

def test_paper_fees_average_entry_and_fills():
    b = PaperBroker(100, .001)
    x = b.buy("BTCUSDT", 20, 10)
    assert x["qty"] == 2
    assert b.cash == pytest.approx(79.98)
    b.buy("BTCUSDT", 20, 20)
    assert b.positions["BTCUSDT"]["qty"] == pytest.approx(3)
    assert b.positions["BTCUSDT"]["avg_price"] == pytest.approx(40/3)
    b.sell("BTCUSDT", 1, 12)
    assert b.cash == pytest.approx(71.948)

def test_no_overspend():
    with pytest.raises(ValueError): PaperBroker(10).buy("BTCUSDT", 11, 10)

def test_equity_marks_positions():
    b = PaperBroker(100)
    b.buy("BTCUSDT", 20, 10)
    assert b.equity({"BTCUSDT": 12}) == pytest.approx(103.98)
