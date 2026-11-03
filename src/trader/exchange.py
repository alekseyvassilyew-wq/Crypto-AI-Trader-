"""Kraken Spot market-data and private read-only API client.

No order placement, withdrawal, deposit, transfer, or funding methods exist here.
"""
import base64
import hashlib
import hmac
import time
import urllib.parse
import requests
import threading

BASE_URL = "https://api.kraken.com"

class KrakenSpot:
    def __init__(self, api_key="", api_secret="", timeout=15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.session = requests.Session()
        self._pair_cache = {}
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def _request(self, path, data=None, private=False):
        data = data or {}
        url = BASE_URL + path
        headers = {}
        if private:
            if not self.api_key or not self.api_secret:
                raise RuntimeError("KRAKEN_API_KEY and KRAKEN_API_SECRET are required for private account checks.")
            # Kraken requires strictly increasing nonces for a given API key.
            with self._nonce_lock:
                self._last_nonce = max(int(time.time() * 1000), self._last_nonce + 1)
                nonce = str(self._last_nonce)
            payload = {"nonce": nonce, **data}
            postdata = urllib.parse.urlencode(payload)
            encoded = (nonce + postdata).encode()
            message = path.encode() + hashlib.sha256(encoded).digest()
            secret = base64.b64decode(self.api_secret, validate=True)
            signature = hmac.new(secret, message, hashlib.sha512).digest()
            headers = {"API-Key": self.api_key, "API-Sign": base64.b64encode(signature).decode()}
            response = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
        else:
            response = self.session.get(url, params=data, timeout=self.timeout)
        response.raise_for_status()
        try:
            result = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as exc:
            raise RuntimeError("Kraken returned a non-JSON response") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Kraken returned an unexpected response format")
        errors = result.get("error") or []
        if errors:
            raise RuntimeError("Kraken API: " + "; ".join(errors))
        return result.get("result", {})

    def _pair_info(self, pair):
        pair = pair.upper().replace("/", "")
        if pair not in self._pair_cache:
            result = self._request("/0/public/AssetPairs", {"pair": pair})
            if not result:
                raise RuntimeError(f"Kraken does not recognize pair {pair}")
            key, info = next(iter(result.items()))
            self._pair_cache[pair] = (key, info)
        return self._pair_cache[pair]

    def tickers(self, symbols):
        out = []
        for symbol in symbols:
            key, info = self._pair_info(symbol)
            result = self._request("/0/public/Ticker", {"pair": key})
            ticker = result.get(key) or next(iter(result.values()))
            bid = float(ticker["b"][0]); ask = float(ticker["a"][0]); last = float(ticker["c"][0])
            base_volume_24h = float(ticker["v"][1])
            out.append({
                "symbol": symbol,
                "bid1Price": bid,
                "ask1Price": ask,
                "lastPrice": last,
                "turnover24h": base_volume_24h * last,
            })
        return out

    def candles(self, symbol, interval="60", limit=100):
        key, _ = self._pair_info(symbol)
        result = self._request("/0/public/OHLC", {"pair": key, "interval": str(interval)})
        rows = result.get(key)
        if rows is None:
            rows = next((v for k, v in result.items() if k != "last"), [])
        # Kraken returns oldest first; retain only completed candles and normalize fields.
        rows = rows[:-1] if len(rows) > 1 else rows
        normalized = []
        for row in rows[-limit:]:
            if len(row) < 7:
                continue
            try:
                normalized.append([float(row[0]), float(row[1]), float(row[2]),
                                   float(row[3]), float(row[4]), float(row[6])])
            except (TypeError, ValueError):
                continue
        if not normalized:
            raise RuntimeError(f"Kraken returned no usable OHLC candles for {symbol}")
        return normalized

    def balance(self):
        """Read account balances only; requires Query Funds permission."""
        return self._request("/0/private/Balance", private=True)
