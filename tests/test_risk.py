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


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_order_values_are_rejected(bad_value):
    with pytest.raises(ValueError, match="finite"):
        check_order(bad_value, 1, 2000, 0, 0, L)
    with pytest.raises(ValueError, match="finite"):
        check_order(5, bad_value, 2000, 0, 0, L)
    with pytest.raises(ValueError, match="finite"):
        check_order(5, 1, bad_value, 0, 0, L)


def test_invalid_position_count_is_rejected():
    with pytest.raises(ValueError, match="non-negative integer"):
        check_order(5, 1, 2000, 1.5, 0, L)
