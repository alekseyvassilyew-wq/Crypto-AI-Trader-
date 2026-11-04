"""Read-only normalization and conservative EUR valuation for Kraken balances."""
import math


_KRAKEN_ASSET_ALIASES = {
    "XXBT": "XBT", "BTC": "XBT", "XBT": "XBT",
    "XETH": "ETH", "ETH": "ETH",
    "ZEUR": "EUR", "ZUSD": "USD", "ZGBP": "GBP",
    "ZCAD": "CAD", "ZJPY": "JPY", "ZCHF": "CHF", "ZAUD": "AUD",
    "XXRP": "XRP", "XXLM": "XLM", "XXDG": "DOGE", "XDG": "DOGE",
}


def normalize_kraken_asset_code(code, known_assets=None):
    """Normalize Kraken legacy codes, using discovered market symbols when available."""
    normalized = str(code or "").strip().upper()
    known = {str(asset).strip().upper() for asset in (known_assets or ()) if asset}
    # An exact live-market symbol always wins over legacy aliases (for example XDG).
    if normalized in known:
        return normalized
    alias = _KRAKEN_ASSET_ALIASES.get(normalized)
    if alias and (not known or alias in known):
        return alias
    # Kraken's legacy X/Z prefix convention is not limited to a fixed list.
    # Strip one prefix only when the remainder is an actual discovered market asset.
    if len(normalized) > 1 and normalized[0] in {"X", "Z"} and normalized[1:] in known:
        return normalized[1:]
    return alias if alias and not known else normalized


def _market_asset_symbols(market_pairs):
    symbols = set()
    for pair in market_pairs or []:
        if isinstance(pair, dict):
            for field in ("base", "quote"):
                value = str(pair.get(field) or "").strip().upper()
                if value:
                    symbols.add(value)
    return symbols


def build_asset_pair_routes(assets, market_pairs):
    """Find online spot pairs involving each wallet asset; retain exchange limits."""
    pairs = [pair for pair in (market_pairs or []) if isinstance(pair, dict)]
    known_assets = _market_asset_symbols(pairs)
    asset_codes = {
        normalize_kraken_asset_code(code, known_assets)
        for code in assets
    }
    routes = {code: [] for code in sorted(asset_codes)}
    for pair in pairs:
        base = normalize_kraken_asset_code(pair.get("base"), known_assets)
        quote = normalize_kraken_asset_code(pair.get("quote"), known_assets)
        if not base or not quote:
            continue
        for asset in asset_codes.intersection({base, quote}):
            routes[asset].append({
                "symbol": str(pair.get("symbol") or ""),
                "base": base,
                "quote": quote,
                "role": "base" if asset == base else "quote",
                "ordermin": pair.get("ordermin"),
                "costmin": pair.get("costmin"),
                "lot_decimals": pair.get("lot_decimals"),
                "pair_decimals": pair.get("pair_decimals"),
                "fees": pair.get("fees"),
                "fees_maker": pair.get("fees_maker"),
            })
    for asset in routes:
        routes[asset].sort(key=lambda item: item["symbol"])
    return routes


def build_wallet_inventory(balances, eur_rates, known_assets=None):
    """Return positive balances with optional EUR marks; never infer missing FX as zero."""
    if not isinstance(balances, dict):
        raise ValueError("Kraken balance response must be an object")
    known = {str(asset).strip().upper() for asset in (known_assets or ()) if asset}
    rates = {"EUR": 1.0}
    for currency, raw_rate in (eur_rates or {}).items():
        code = normalize_kraken_asset_code(currency, known)
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError, OverflowError):
            continue
        if code and math.isfinite(rate) and rate > 0:
            rates[code] = rate

    assets = []
    for raw_code, raw_amount in sorted(balances.items(), key=lambda item: str(item[0]).upper()):
        code = str(raw_code).strip().upper()
        canonical_code = normalize_kraken_asset_code(code, known)
        if not code:
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(amount) or amount <= 0:
            continue
        rate = rates.get(canonical_code)
        value_eur = amount * rate if rate is not None else None
        if value_eur is not None and not math.isfinite(value_eur):
            value_eur = None
        assets.append({
            "asset": code,
            "canonical_asset": canonical_code,
            "amount": amount,
            "eur_rate": rate,
            "value_eur": value_eur,
        })

    known_total = sum(item["value_eur"] for item in assets if item["value_eur"] is not None)
    unknown_assets = [item["asset"] for item in assets if item["value_eur"] is None]
    return {
        "assets": assets,
        "asset_count": len(assets),
        "known_total_eur": known_total,
        "unknown_assets": unknown_assets,
        "total_complete": not unknown_assets,
        "total_eur": known_total if not unknown_assets else None,
    }
