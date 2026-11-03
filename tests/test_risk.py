import pytest
from src.trader.risk import Limits, check_order
L = Limits(10, 5, 3, 20, 1000)

def test_size_spread_liquidity_and_position_caps():
    for args in [(11, 1, 2000, 0, 0), (5, 21, 2000, 0, 0), (5, 1, 10, 0, 0), (5, 1, 2000, 3, 0)]:
        with pytest.raises(ValueError): check_order(*args, L)

def test_daily_loss_cap():
    with pytest.raises(PermissionError): check_order(5, 1, 2000, 0, -5, L)

def test_valid_order():
    check_order(5, 1, 2000, 0, 0, L)
