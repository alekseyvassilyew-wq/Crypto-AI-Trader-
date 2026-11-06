import pytest

from src.trader.account_sync import sync_kraken_account
from src.trader.db import connect, get_kraken_account_sync


class FakeKraken:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def _call(self, name, result):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("read failed")
        return result

    def balance(self):
        return self._call("balance", {"ZEUR": "100"})

    def trades_history(self):
        return self._call("trades_history", [{
            "id": "TX1", "ordertxid": "O1", "pair": "XBTEUR",
            "time": "1794000000", "type": "buy", "ordertype": "limit",
            "price": "50000", "cost": "100", "fee": "0.26", "vol": "0.002",
        }])

    def ledger_history(self):
        return self._call("ledger_history", [{
            "id": "L1", "refid": "O1", "time": "1794000000",
            "type": "trade", "asset": "ZEUR", "amount": "-100",
            "fee": "0.26", "balance": "100",
        }])


def test_sync_kraken_account_reads_all_sources_and_persists_snapshot():
    db = connect(":memory:")
    exchange = FakeKraken()
    result = sync_kraken_account(exchange, db, fetched_at="2026-11-06T20:00:00Z")

    assert exchange.calls == ["balance", "trades_history", "ledger_history"]
    assert result["status"] == "COMPLETE"
    assert get_kraken_account_sync(db)["balance"] == {"ZEUR": 100.0}
    db.close()


@pytest.mark.parametrize("failure", ["balance", "trades_history", "ledger_history"])
def test_sync_does_not_write_partial_snapshot_when_any_read_fails(failure):
    db = connect(":memory:")
    with pytest.raises(RuntimeError, match="read failed"):
        sync_kraken_account(FakeKraken(fail=failure), db)
    assert get_kraken_account_sync(db)["status"] == "UNKNOWN"
    assert db.execute("SELECT COUNT(*) FROM kraken_fills").fetchone()[0] == 0
    db.close()
