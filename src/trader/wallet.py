"""Read-only normalization and conservative EUR valuation for Kraken balances."""
import math


_KRAKEN_ASSET_ALIASES = {
    "XXBT": "XBT", "BTC": "XBT", "XBT": "XBT",
    "XETH": "ETH", "ETH": "ETH",
    "ZEUR": "EUR", "ZUSD": "USD", "ZGBP": "GBP",
    "ZCAD": "CAD", "ZJPY": "JPY", "ZCHF": "CHF", "ZAUD": "AUD",
    "XXRP": "XRP", "XXLM": "XLM", "XXDG": "DOGE", "XDG": "DOGE",
}


def normalize_kraken_asset_code(code):
    """Normalize common Kraken legacy asset codes to market-universe symbols."""
    normalized = str(code or "").strip().upper()
    return _KRAKEN_ASSET_ALIASES.get(normalized, normalized)


def build_asset_pair_routes(assets, market_pairs):
    """Find online spot pairs involving each wallet asset; retain exchange limits."""
    asset_codes = {normalize_kraken_asset_code(code) for code in assets}
    routes = {code: [] for code in sorted(asset_codes)}
    for pair in market_pairs or []:
        if not isinstance(pair, dict):
            continue
        base = normalize_kraken_asset_code(pair.get("base"))
        quote = normalize_kraken_asset_code(pair.get("quote"))
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


def build_wallet_inventory(balances, eur_rates):
    """Return positive balances with optional EUR marks; never infer missing FX as zero."""
    if not isinstance(balances, dict):
        raise ValueError("Kraken balance response must be an object")
    rates = {"EUR": 1.0}
    for currency, raw_rate in (eur_rates or {}).items():
        code = str(currency).strip().upper()
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError, OverflowError):
            continue
        if code and math.isfinite(rate) and rate > 0:
            rates[code] = rate

    assets = []
    for raw_code, raw_amount in sorted(balances.items(), key=lambda item: str(item[0]).upper()):
        code = normalize_kraken_asset_code(raw_code)
        if not code:
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(amount) or amount <= 0:
            continue
        rate = rates.get(code)
        value_eur = amount * rate if rate is not None else None
        if value_eur is not None and not math.isfinite(value_eur):
            value_eur = None
        assets.append({
            "asset": code,
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
