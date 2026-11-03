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
