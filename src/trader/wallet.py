"""Read-only normalization and conservative EUR valuation for Kraken balances."""
import math


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
        code = str(raw_code).strip().upper()
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
