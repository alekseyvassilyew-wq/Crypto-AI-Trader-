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
