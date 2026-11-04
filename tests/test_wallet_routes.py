import pytest

from src.trader.wallet import (
    build_asset_pair_routes,
    build_wallet_inventory,
    normalize_kraken_asset_code,
)


def test_kraken_legacy_asset_codes_normalize_for_market_matching():
    assert normalize_kraken_asset_code("XXBT") == "XBT"
    assert normalize_kraken_asset_code("XETH") == "ETH"
    assert normalize_kraken_asset_code("ZEUR") == "EUR"
    assert normalize_kraken_asset_code("ZUSD") == "USD"
    assert normalize_kraken_asset_code("SOMECOIN") == "SOMECOIN"


def test_inventory_keeps_original_balance_code_but_uses_normalized_fx_key():
    result = build_wallet_inventory(
        {"XXBT": "0.01", "ZEUR": "20"},
        {"XBT": 60000, "EUR": 1},
    )
    by_asset = {item["asset"]: item for item in result["assets"]}
    assert by_asset["XXBT"]["canonical_asset"] == "XBT"
    assert by_asset["XXBT"]["value_eur"] == pytest.approx(600)
    assert by_asset["ZEUR"]["value_eur"] == pytest.approx(20)
    assert result["total_eur"] == pytest.approx(620)


def test_asset_pair_routes_include_base_and_quote_markets_and_constraints():
    routes = build_asset_pair_routes(
        ["XXBT", "ZEUR"],
        [
            {"symbol": "XBTEUR", "base": "XBT", "quote": "EUR",
             "ordermin": "0.0001", "costmin": "0.5", "fees": [[0, 0.26]]},
            {"symbol": "ETHEUR", "base": "ETH", "quote": "EUR"},
            {"symbol": "BTCUSD", "base": "XBT", "quote": "USD"},
        ],
    )
    assert {item["symbol"] for item in routes["XBT"]} == {"XBTEUR", "BTCUSD"}
    xbteur = next(item for item in routes["XBT"] if item["symbol"] == "XBTEUR")
    assert xbteur["ordermin"] == "0.0001"
    assert xbteur["fees"] == [[0, 0.26]]
    assert [item["symbol"] for item in routes["EUR"]] == ["XBTEUR"]
    assert routes["EUR"][0]["role"] == "quote"
