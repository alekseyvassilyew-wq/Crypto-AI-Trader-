from src.trader.indicators import ema,rsi
def test_ema_has_value(): assert ema([1,2,3,4,5],3) is not None
def test_flat_rsi(): assert rsi([10]*20)==50
