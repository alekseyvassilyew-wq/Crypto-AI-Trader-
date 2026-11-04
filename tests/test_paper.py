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


def test_buy_and_sell_preserve_cash_and_position():
    broker = PaperBroker(balance=20, fee_rate=0.01)
    bought = broker.buy("XBTEUR", 10, 2)
    assert bought["qty"] == 5
    assert broker.cash == pytest.approx(9.9)
    sold = broker.sell("XBTEUR", 2, 3)
    assert sold["quote"] == 6
    assert broker.cash == pytest.approx(15.84)
    assert broker.positions["XBTEUR"]["qty"] == pytest.approx(3)


@pytest.mark.parametrize("balance,fee_rate", [
    (float("nan"), 0.001), (float("inf"), 0.001), (-1, 0.001),
    (10, float("nan")), (10, -0.1), (10, 0.1),
])
def test_constructor_rejects_invalid_account_settings(balance, fee_rate):
    with pytest.raises(ValueError):
        PaperBroker(balance=balance, fee_rate=fee_rate)


@pytest.mark.parametrize("quote,price", [
    (float("nan"), 1), (1, float("inf")), (-1, 1), (1, 0),
])
def test_buy_rejects_invalid_amounts_without_mutating_account(quote, price):
    broker = PaperBroker(balance=20)
    before_cash = broker.cash
    with pytest.raises(ValueError):
        broker.buy("XBTEUR", quote, price)
    assert broker.cash == before_cash
    assert broker.positions == {}


def test_sell_rejects_more_than_held_without_mutating_account():
    broker = PaperBroker(balance=20)
    broker.buy("XBTEUR", 5, 10)
    before_cash = broker.cash
    before_position = dict(broker.positions["XBTEUR"])
    with pytest.raises(ValueError):
        broker.sell("XBTEUR", 1, 10)
    assert broker.cash == before_cash
    assert broker.positions["XBTEUR"] == before_position


def test_equity_rejects_non_finite_mark_price():
    broker = PaperBroker(balance=20)
    broker.buy("XBTEUR", 5, 10)
    with pytest.raises(ValueError):
        broker.equity({"XBTEUR": float("nan")})


def test_load_rejects_corrupted_persisted_position(tmp_path):
    import json
    from src.trader.db import connect
    db = connect(tmp_path / "corrupt.sqlite3")
    # Simulate legacy/corrupt data written outside the validated state helper.
    payload = json.dumps({
        "cash": 10,
        "positions": {"XBTEUR": {"qty": float("nan"), "avg_price": 10}},
    })
    db.execute("INSERT INTO app_state(key, value) VALUES(?, ?)", ("paper_broker", payload))
    db.commit()
    with pytest.raises(ValueError):
        PaperBroker(db=db)
    db.close()


def test_multi_currency_wallet_debits_and_credits_matching_quote_currency():
    broker = PaperBroker(balance=20, fee_rate=0.01)
    broker.cash_by_currency["USD"] = 50
    bought = broker.buy("ETHUSD", 20, 10, currency="USD")
    assert bought["currency"] == "USD"
    assert broker.balance("USD") == pytest.approx(29.8)
    assert broker.cash == pytest.approx(20)
    sold = broker.sell("ETHUSD", 1, 12)
    assert sold["currency"] == "USD"
    assert broker.balance("USD") == pytest.approx(41.68)
    assert broker.cash == pytest.approx(20)

 
 
def test_paper_currency_conversion_moves_virtual_cash_and_charges_fee():
    broker = PaperBroker(balance=20, fee_rate=0.01)
    converted = broker.convert("EUR", "USD", 10, 1.1)
    assert converted["spent"] == pytest.approx(10)
    assert converted["received"] == pytest.approx(10.89)
    assert broker.balance("EUR") == pytest.approx(10)
    assert broker.balance("USD") == pytest.approx(10.89)


def test_multi_currency_equity_uses_fx_rates():
    broker = PaperBroker(balance=20)
    broker.cash_by_currency["USD"] = 10
    assert broker.equity({}, {"USD": 0.9}) == pytest.approx(29)