"""Kraken Spot market-data and private read-only API client.

No order placement, withdrawal, deposit, transfer, or funding methods exist here.
"""
import base64
import hashlib
import hmac
import math
import threading
import time
import urllib.parse

import requests


BASE_URL = "https://api.kraken.com"


def _finite_number(value, field, *, positive=False, non_negative=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid Kraken {field}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Invalid Kraken {field}: value must be finite")
    if positive and number <= 0:
        raise ValueError(f"Invalid Kraken {field}: value must be positive")
    if non_negative and number < 0:
        raise ValueError(f"Invalid Kraken {field}: value must not be negative")
    return number


class KrakenSpot:
    def __init__(self, api_key="", api_secret="", timeout=15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = _finite_number(timeout, "request timeout", positive=True)
        self.session = requests.Session()
        self._pair_cache = {}
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0
        self._public_lock = threading.Lock()
        self._last_public_request = 0.0

    def _request(self, path, data=None, private=False):
        data = data or {}
        url = BASE_URL + path
        headers = {}
        if private:
            if not self.api_key or not self.api_secret:
                raise RuntimeError("KRAKEN_API_KEY and KRAKEN_API_SECRET are required for private account checks.")
            with self._nonce_lock:
                self._last_nonce = max(int(time.time() * 1000), self._last_nonce + 1)
                nonce = str(self._last_nonce)
            payload = {"nonce": nonce, **data}
            postdata = urllib.parse.urlencode(payload)
            encoded = (nonce + postdata).encode()
            message = path.encode() + hashlib.sha256(encoded).digest()
            try:
                secret = base64.b64decode(self.api_secret, validate=True)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("KRAKEN_API_SECRET must be valid base64") from exc
            signature = hmac.new(secret, message, hashlib.sha512).digest()
            headers = {"API-Key": self.api_key, "API-Sign": base64.b64encode(signature).decode()}
        try:
            if private:
                response = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
            else:
                # Kraken public REST endpoints share a per-IP call budget.
                # Serialize requests and leave a small gap to reduce throttling.
                with self._public_lock:
                    wait = 0.5 - (time.monotonic() - self._last_public_request)
                    if wait > 0:
                        time.sleep(wait)
                    self._last_public_request = time.monotonic()
                    response = self.session.get(url, params=data, timeout=self.timeout)
            response.raise_for_status()
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RuntimeError(f"Kraken network request failed: {exc}") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("Kraken returned a non-JSON response") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Kraken returned an unexpected response format")
        errors = result.get("error") or []
        if not isinstance(errors, list):
            raise RuntimeError("Kraken returned an invalid error field")
        if errors:
            raise RuntimeError("Kraken API: " + "; ".join(str(error) for error in errors))
        payload = result.get("result")
        if not isinstance(payload, dict):
            raise RuntimeError("Kraken returned an invalid result payload")
        return payload

    def _pair_info(self, pair):
        pair = str(pair).upper().replace("/", "")
        if not pair:
            raise ValueError("Trading pair must not be empty")
        if pair not in self._pair_cache:
            result = self._request("/0/public/AssetPairs", {"pair": pair})
            if not result:
                raise RuntimeError(f"Kraken does not recognize pair {pair}")
            key, info = next(iter(result.items()))
            if not isinstance(info, dict):
                raise RuntimeError(f"Kraken returned invalid pair metadata for {pair}")
            self._pair_cache[pair] = (key, info)
        return self._pair_cache[pair]

    def discover_pairs(self, force=False):
        """Return every online spot crypto/crypto or crypto/fiat market on Kraken."""
        now = time.monotonic()
        cached_at = getattr(self, "_universe_cached_at", 0)
        cached = getattr(self, "_market_universe", None)
        if cached is not None and not force and now - cached_at < 3600:
            return list(cached)

        result = self._request("/0/public/AssetPairs")
        pairs = []
        pair_cache = {}
        for pair_key, info in result.items():
            if not isinstance(info, dict):
                continue
            if info.get("aclass_base") != "currency" or info.get("aclass_quote") != "currency":
                continue
            wsname = str(info.get("wsname") or "")
            altname = str(info.get("altname") or "")
            status = str(info.get("status") or "online").lower()
            if status != "online" or not wsname or "/" not in wsname:
                continue
            # Dark-pool markets are not part of the normal public spot universe.
            if wsname.lower().endswith(".d") or altname.lower().endswith(".d"):
                continue
            base, quote = wsname.split("/", 1)
            symbol = altname.upper().replace("/", "") or (base + quote).upper()
            if not symbol or not base or not quote:
                continue
            meta = {
                "symbol": symbol,
                "pair_key": pair_key,
                "base": base.upper(),
                "quote": quote.upper(),
                "status": status,
            }
            pairs.append(meta)
            pair_cache[symbol] = (pair_key, info)
            pair_cache[pair_key] = (pair_key, info)

        if not pairs:
            raise RuntimeError("Kraken returned no online spot markets")
        self._pair_cache.update(pair_cache)
        self._market_universe = sorted(pairs, key=lambda item: item["symbol"])
        self._universe_cached_at = now
        return list(self._market_universe)

    def tickers(self, symbols=None):
        """Fetch tickers for selected symbols, or all Kraken markets when omitted."""
        if symbols is None:
            pairs = self.discover_pairs()
            result = self._request("/0/public/Ticker")
            by_key = {item["pair_key"]: item for item in pairs}
            out = []
            for pair_key, item in by_key.items():
                ticker = result.get(pair_key)
                if not isinstance(ticker, dict):
                    continue
                try:
                    bid = _finite_number(ticker["b"][0], "bid", positive=True)
                    ask = _finite_number(ticker["a"][0], "ask", positive=True)
                    last = _finite_number(ticker["c"][0], "last price", positive=True)
                    base_volume_24h = _finite_number(ticker["v"][1], "24h volume", non_negative=True)
                except (KeyError, IndexError, TypeError, ValueError, OverflowError):
                    # One inactive/illiquid market may expose a zero or malformed
                    # last price. Skip that ticker without aborting the full scan.
                    continue
                if ask < bid:
                    continue
                turnover = _finite_number(base_volume_24h * last, "24h turnover", non_negative=True)
                out.append({
                    "symbol": item["symbol"],
                    "base": item["base"],
                    "quote": item["quote"],
                    "bid1Price": bid,
                    "ask1Price": ask,
                    "lastPrice": last,
                    "turnover24h": turnover,
                })
            return out

        out = []
        for symbol in symbols:
            key, info = self._pair_info(symbol)
            result = self._request("/0/public/Ticker", {"pair": key})
            ticker = result.get(key) or (next(iter(result.values())) if result else None)
            if not isinstance(ticker, dict):
                raise RuntimeError(f"Kraken returned no ticker data for {symbol}")
            try:
                bid = _finite_number(ticker["b"][0], "bid", positive=True)
                ask = _finite_number(ticker["a"][0], "ask", positive=True)
                last = _finite_number(ticker["c"][0], "last price", positive=True)
                base_volume_24h = _finite_number(ticker["v"][1], "24h volume", non_negative=True)
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"Kraken returned malformed ticker data for {symbol}") from exc
            if ask < bid:
                raise RuntimeError(f"Kraken returned crossed bid/ask prices for {symbol}")
            turnover = _finite_number(base_volume_24h * last, "24h turnover", non_negative=True)
            out.append({
                "symbol": symbol,
                "bid1Price": bid,
                "ask1Price": ask,
                "lastPrice": last,
                "turnover24h": turnover,
            })
        return out

    def candles(self, symbol, interval="60", limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Candle limit must be a positive integer")
        key, _ = self._pair_info(symbol)
        result = self._request("/0/public/OHLC", {"pair": key, "interval": str(interval)})
        rows = result.get(key)
        if rows is None:
            rows = next((v for k, v in result.items() if k != "last"), [])
        if not isinstance(rows, list):
            raise RuntimeError(f"Kraken returned malformed OHLC data for {symbol}")
        # Kraken returns oldest first; the final candle is still open.
        rows = rows[:-1] if len(rows) > 1 else rows
        normalized = []
        for row in rows[-limit:]:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            try:
                timestamp = _finite_number(row[0], "candle timestamp", non_negative=True)
                open_price = _finite_number(row[1], "candle open", positive=True)
                high = _finite_number(row[2], "candle high", positive=True)
                low = _finite_number(row[3], "candle low", positive=True)
                close = _finite_number(row[4], "candle close", positive=True)
                volume = _finite_number(row[6], "candle volume", non_negative=True)
            except ValueError:
                continue
            if low > min(open_price, close) or high < max(open_price, close) or low > high:
                continue
            normalized.append([timestamp, open_price, high, low, close, volume])
        if not normalized:
            raise RuntimeError(f"Kraken returned no usable OHLC candles for {symbol}")
        return normalized

    def balance(self):
        """Read account balances only; requires Query Funds permission."""
        return self._request("/0/private/Balance", private=True)
