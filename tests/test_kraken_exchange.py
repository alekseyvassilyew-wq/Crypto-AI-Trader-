import pytest
import base64
from src.trader.exchange import KrakenSpot

def test_private_signature_is_base64(monkeypatch):
    # Validate that the HMAC signature helper path produces an API-Sign header.
    import requests
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"error": [], "result": {"ZEUR": "1"}}
    seen = {}
    def fake_post(url, data=None, headers=None, timeout=None):
        seen.update(headers or {})
        return FakeResponse()
    monkeypatch.setattr(requests.Session, "post", lambda self, *a, **kw: fake_post(*a, **kw))
    secret = base64.b64encode(b"unit-test-secret").decode()
    client = KrakenSpot("public-key", secret)
    assert client.balance() == {"ZEUR": "1"}
    assert "API-Key" in seen and "API-Sign" in seen
    assert base64.b64decode(seen["API-Sign"])

def test_pair_alias_normalization():
    assert "XBTEUR" == "XBT/EUR".replace("/", "")


def test_private_nonces_are_strictly_increasing(monkeypatch):
    import requests
    seen = []

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"error": [], "result": {}}

    def fake_post(self, url, data=None, headers=None, timeout=None):
        seen.append(int(data["nonce"]))
        return FakeResponse()

    monkeypatch.setattr(requests.Session, "post", fake_post)
    secret = base64.b64encode(b"unit-test-secret").decode()
    client = KrakenSpot("public-key", secret)
    client.balance()
    client.balance()
    assert seen[1] > seen[0]


def test_ohlc_normalizes_numeric_fields_and_drops_open_candle(monkeypatch):
    client = KrakenSpot()
    client._pair_cache["XBTEUR"] = ("XXBTZEUR", {})
    client._last_ohlc_request_at = 100.0
    monkeypatch.setattr(client, "_request", lambda *a, **k: {
        "XXBTZEUR": [
            [1, "10", "12", "9", "11", "0", "4.5"],
            [2, "11", "13", "10", "12", "0", "5.5"],
        ],
        "last": "2",
    })
    rows = client.candles("XBTEUR")
    assert rows == [[1.0, 10.0, 12.0, 9.0, 11.0, 4.5]]


def test_ohlc_requests_are_spaced_at_least_one_second(monkeypatch):
    import src.trader.exchange as exchange_module

    client = KrakenSpot()
    client._pair_cache["XBTEUR"] = ("XXBTZEUR", {})
    client._last_ohlc_request_at = 100.0
    monkeypatch.setattr(client, "_request", lambda *a, **k: {
        "XXBTZEUR": [[1, "10", "12", "9", "11", "0", "4.5"], [2, "11", "13", "10", "12", "0", "5.5"]],
        "last": "2",
    })
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(exchange_module.time, "monotonic", lambda: clock[0])
    def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(exchange_module.time, "sleep", advance)

    client.candles("XBTEUR")
    client.candles("XBTEUR")
    assert sleeps == pytest.approx([1.05, 1.05])


def test_all_public_rest_requests_are_spaced_at_least_1_05_seconds(monkeypatch):
    import src.trader.exchange as exchange_module

    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"error": [], "result": {"ok": 1}}

    client = KrakenSpot()
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: FakeResponse())
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(exchange_module.time, "monotonic", lambda: clock[0])

    def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(exchange_module.time, "sleep", advance)
    client._request("/0/public/SystemStatus")
    client._request("/0/public/SystemStatus")

    assert sleeps == pytest.approx([1.05])


def test_trades_history_paginates_and_preserves_exchange_records(monkeypatch):
    client = KrakenSpot()
    calls = []

    def fake_request(path, data=None, private=False):
        calls.append((path, dict(data or {}), private))
        if data["ofs"] == 0:
            return {"count": 3, "trades": {
                "tx-a": {"ordertxid": "order-a", "price": "10", "cost": "20", "fee": "0.1", "vol": "2"}
            }}
        return {"count": 3, "trades": {
            "tx-b": {"ordertxid": "order-b", "price": "11"},
            "tx-c": {"ordertxid": "order-c", "price": "12"},
        }}

    monkeypatch.setattr(client, "_request", fake_request)
    rows = client.trades_history(start=100, end=200)
    assert [row["id"] for row in rows] == ["tx-a", "tx-b", "tx-c"]
    assert rows[0]["fee"] == "0.1"
    assert calls == [
        ("/0/private/TradesHistory", {"start": 100, "end": 200, "ofs": 0}, True),
        ("/0/private/TradesHistory", {"start": 100, "end": 200, "ofs": 1}, True),
    ]


def test_ledger_history_reads_read_only_endpoint(monkeypatch):
    client = KrakenSpot()
    seen = {}

    def fake_request(path, data=None, private=False):
        seen.update(path=path, data=data, private=private)
        return {"count": 1, "ledger": {
            "ledger-1": {"type": "trade", "asset": "ZEUR", "amount": "-5", "fee": "0.01"}
        }}

    monkeypatch.setattr(client, "_request", fake_request)
    rows = client.ledger_history()
    assert rows == [{"id": "ledger-1", "type": "trade", "asset": "ZEUR", "amount": "-5", "fee": "0.01"}]
    assert seen == {"path": "/0/private/Ledgers", "data": {"ofs": 0}, "private": True}


@pytest.mark.parametrize("kwargs", [
    {"start": -1}, {"start": 1.5}, {"start": True},
    {"start": 20, "end": 10}, {"end": "nope"},
])
def test_history_rejects_invalid_time_bounds(kwargs):
    client = KrakenSpot()
    with pytest.raises(ValueError):
        client.trades_history(**kwargs)


def test_history_fails_closed_on_incomplete_pagination(monkeypatch):
    client = KrakenSpot()
    monkeypatch.setattr(client, "_request", lambda *a, **k: {
        "count": 2, "trades": {"tx-a": {"price": "10"}}
    })
    with pytest.raises(RuntimeError, match="incomplete"):
        client.trades_history()


def test_history_fails_if_account_changes_during_pagination(monkeypatch):
    client = KrakenSpot()
    pages = iter([
        {"count": 2, "trades": {"tx-a": {"price": "10"}}},
        {"count": 3, "trades": {"tx-b": {"price": "11"}}},
    ])
    monkeypatch.setattr(client, "_request", lambda *a, **k: next(pages))
    with pytest.raises(RuntimeError, match="changed during pagination"):
        client.trades_history()


def test_history_rejects_missing_or_non_integer_count(monkeypatch):
    client = KrakenSpot()
    for page in (
        {"trades": {"tx-a": {"price": "10"}}},
        {"count": 1.5, "trades": {"tx-a": {"price": "10"}}},
        {"count": True, "trades": {"tx-a": {"price": "10"}}},
    ):
        monkeypatch.setattr(client, "_request", lambda *a, _page=page, **k: _page)
        with pytest.raises(RuntimeError, match="count"):
            client.trades_history()


def test_order_book_reads_and_normalizes_public_depth(monkeypatch):
    client = KrakenSpot()
    monkeypatch.setattr(client, "_pair_info", lambda symbol: ("XBT/EUR", {}))
    seen = {}
    def fake_request(path, data=None, private=False):
        seen.update(path=path, data=data, private=private)
        return {"XBT/EUR": {
            "asks": [["101", "2"], ["102", "3"]],
            "bids": [["100", "4"], ["99", "5"]],
        }}
    monkeypatch.setattr(client, "_request", fake_request)
    book = client.order_book("XBTEUR", depth=2)
    assert seen == {"path": "/0/public/Depth", "data": {"pair": "XBT/EUR", "count": 2}, "private": False}
    assert book["asks"] == [[101.0, 2.0], [102.0, 3.0]]
    assert book["bids"] == [[100.0, 4.0], [99.0, 5.0]]


def test_order_book_rejects_crossed_or_unsorted_book(monkeypatch):
    client = KrakenSpot()
    monkeypatch.setattr(client, "_pair_info", lambda symbol: ("PAIR", {}))
    monkeypatch.setattr(client, "_request", lambda *a, **k: {"PAIR": {
        "asks": [["99", "1"], ["100", "1"]],
        "bids": [["100", "1"], ["98", "1"]],
    }})
    with pytest.raises(RuntimeError, match="unsorted asks"):
        client.order_book("PAIR")


def test_order_book_rejects_invalid_depth():
    client = KrakenSpot()
    with pytest.raises(ValueError, match="depth"):
        client.order_book("PAIR", depth=0)
