import base64
import hashlib
import hmac
import urllib.parse

import pytest
import requests

from src.trader.exchange import KrakenSpot


class FakeResponse:
    def __init__(self, payload, status_error=None):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.response

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.response


def test_public_request_returns_result_and_uses_timeout():
    client = KrakenSpot(timeout=7)
    session = FakeSession(FakeResponse({"error": [], "result": {"ok": 1}}))
    client.session = session

    assert client._request("/0/public/SystemStatus") == {"ok": 1}
    args, kwargs = session.get_calls[0]
    assert args[0].endswith("/0/public/SystemStatus")
    assert kwargs["timeout"] == 7


def test_private_request_builds_valid_kraken_signature(monkeypatch):
    secret = base64.b64encode(b"test-secret-key").decode()
    client = KrakenSpot(api_key="public-key", api_secret=secret)
    session = FakeSession(FakeResponse({"error": [], "result": {"Z EUR": "12.5"}}))
    client.session = session
    monkeypatch.setattr("src.trader.exchange.time.time", lambda: 1234.5)

    result = client.balance()

    assert result == {"Z EUR": "12.5"}
    args, kwargs = session.post_calls[0]
    payload = kwargs["data"]
    assert payload["nonce"] == "1234500"
    postdata = urllib.parse.urlencode(payload)
    message = b"/0/private/Balance" + hashlib.sha256(
        (payload["nonce"] + postdata).encode()
    ).digest()
    expected = base64.b64encode(
        hmac.new(b"test-secret-key", message, hashlib.sha512).digest()
    ).decode()
    assert kwargs["headers"]["API-Key"] == "public-key"
    assert kwargs["headers"]["API-Sign"] == expected
    assert kwargs["timeout"] == 15


def test_private_requests_use_strictly_increasing_nonces(monkeypatch):
    client = KrakenSpot(api_key="key", api_secret=base64.b64encode(b"secret").decode())
    session = FakeSession(FakeResponse({"error": [], "result": {}}))
    client.session = session
    monkeypatch.setattr("src.trader.exchange.time.time", lambda: 100)

    client.balance()
    client.balance()

    nonces = [call[1]["data"]["nonce"] for call in session.post_calls]
    assert int(nonces[1]) > int(nonces[0])


def test_private_request_requires_credentials():
    with pytest.raises(RuntimeError, match="KRAKEN_API_KEY"):
        KrakenSpot().balance()


def test_api_errors_are_raised():
    client = KrakenSpot()
    client.session = FakeSession(FakeResponse({
        "error": ["EAPI:Invalid key"],
        "result": {},
    }))
    with pytest.raises(RuntimeError, match="EAPI:Invalid key"):
        client._request("/0/private/Balance", private=True)


def test_non_json_response_is_rejected():
    client = KrakenSpot()
    client.session = FakeSession(FakeResponse(ValueError("bad json")))
    with pytest.raises(RuntimeError, match="non-JSON"):
        client._request("/0/public/SystemStatus")


def test_http_errors_are_propagated():
    client = KrakenSpot()
    client.session = FakeSession(FakeResponse(
        {}, requests.HTTPError("503 Server Error")
    ))
    with pytest.raises(requests.HTTPError):
        client._request("/0/public/SystemStatus")


def test_ticker_normalizes_kraken_response_and_caches_pair():
    client = KrakenSpot()
    session = FakeSession(FakeResponse({"error": [], "result": {
        "XXBTZEUR": {
            "b": ["60000.0", "1", "1.0"],
            "a": ["60010.0", "1", "1.0"],
            "c": ["60005.0", "0.1"],
            "v": ["10.0", "20.0"],
        }
    }}))
    client.session = session

    def fake_request(path, data=None, private=False):
        if path.endswith("AssetPairs"):
            return {"XXBTZEUR": {"altname": "XBTEUR"}}
        return session.response.payload["result"]

    client._request = fake_request
    ticker = client.tickers(["XBTEUR"])[0]

    assert ticker == {
        "symbol": "XBTEUR",
        "bid1Price": 60000.0,
        "ask1Price": 60010.0,
        "lastPrice": 60005.0,
        "turnover24h": 1200100.0,
    }
    client.tickers(["XBTEUR"])
    assert client._pair_cache["XBTEUR"][0] == "XXBTZEUR"


def test_ohlc_excludes_unclosed_candle_and_normalizes_rows():
    client = KrakenSpot()
    client._pair_cache["XBTEUR"] = ("XXBTZEUR", {})
    client._request = lambda *args, **kwargs: {
        "XXBTZEUR": [
            [1000, "10", "12", "9", "11", "10.5", "100", 5],
            [1060, "11", "13", "10", "12", "11.5", "200", 6],
            [1120, "12", "14", "11", "13", "12.5", "300", 7],
        ],
        "last": 123456,
    }

    rows = client.candles("XBTEUR", interval="1", limit=10)

    assert rows == [
        [1000.0, 10.0, 12.0, 9.0, 11.0, 100.0],
        [1060.0, 11.0, 13.0, 10.0, 12.0, 200.0],
    ]


def test_ohlc_rejects_empty_or_unusable_candles():
    client = KrakenSpot()
    client._pair_cache["XBTEUR"] = ("XXBTZEUR", {})
    client._request = lambda *args, **kwargs: {"XXBTZEUR": []}

    with pytest.raises(RuntimeError, match="no usable OHLC"):
        client.candles("XBTEUR")


def test_pair_lookup_rejects_unknown_symbol():
    client = KrakenSpot()
    client._request = lambda *args, **kwargs: {}

    with pytest.raises(RuntimeError, match="does not recognize pair"):
        client._pair_info("INVALID")
