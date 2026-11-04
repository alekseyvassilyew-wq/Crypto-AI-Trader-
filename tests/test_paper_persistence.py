from src.trader.db import connect
from src.trader.paper import PaperBroker

def test_paper_balance_and_positions_survive_reopen(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    db1 = connect(db_path)
    broker1 = PaperBroker(1000, 0.001, db=db1)
    broker1.buy("BTCUSDT", 100, 50000)
    expected_cash = broker1.cash
    expected_positions = dict(broker1.positions)
    db1.close()
    db2 = connect(db_path)
    broker2 = PaperBroker(1000, 0.001, db=db2)
    assert broker2.cash == expected_cash
    assert broker2.positions == expected_positions
    db2.close()


def test_multi_currency_positions_and_accounting_survive_full_reopen(tmp_path):
    db_path = tmp_path / "full-state.sqlite3"
    db1 = connect(db_path)
    broker1 = PaperBroker(100, 0.01, db=db1)
    broker1.convert("EUR", "USD", 20, 1.1)
    broker1.buy("ETHUSD", 10, 5, currency="USD", quote_to_eur_rate=0.9)
    broker1.buy("XBTEUR", 15, 30000, currency="EUR", quote_to_eur_rate=1.0)
    broker1.update_peak("ETHUSD", 6)
    broker1.sell("ETHUSD", 0.5, 7)
    expected_cash = dict(broker1.cash_by_currency)
    expected_positions = {key: dict(value) for key, value in broker1.positions.items()}
    db1.close()

    db2 = connect(db_path)
    broker2 = PaperBroker(100, 0.01, db=db2)
    assert broker2.cash_by_currency == expected_cash
    assert broker2.positions == expected_positions
    assert broker2.positions["ETHUSD"]["peak_price"] == 6
    assert broker2.positions["ETHUSD"]["cost_basis_eur"] == expected_positions["ETHUSD"]["cost_basis_eur"]
    assert broker2.positions["ETHUSD"]["entry_fees_eur"] == expected_positions["ETHUSD"]["entry_fees_eur"]
    db2.close()


def test_multi_currency_positions_and_accounting_survive_full_reopen(tmp_path):
    db_path = tmp_path / "full-state.sqlite3"
    db1 = connect(db_path)
    broker1 = PaperBroker(100, 0.01, db=db1)
    broker1.convert("EUR", "USD", 20, 1.1)
    broker1.buy("ETHUSD", 10, 5, currency="USD", quote_to_eur_rate=0.9)
    broker1.buy("XBTEUR", 15, 30000, currency="EUR", quote_to_eur_rate=1.0)
    broker1.update_peak("ETHUSD", 6)
    broker1.sell("ETHUSD", 0.5, 7)
    expected_cash = dict(broker1.cash_by_currency)
    expected_positions = {key: dict(value) for key, value in broker1.positions.items()}
    db1.close()

    db2 = connect(db_path)
    broker2 = PaperBroker(100, 0.01, db=db2)
    assert broker2.cash_by_currency == expected_cash
    assert broker2.positions == expected_positions
    assert broker2.positions["ETHUSD"]["peak_price"] == 6
    db2.close()
