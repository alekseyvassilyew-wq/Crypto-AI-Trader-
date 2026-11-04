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


def test_unknown_legacy_prefix_is_normalized_only_when_market_confirms_asset():
    assert normalize_kraken_asset_code("XXMR", {"XMR", "EUR"}) == "XMR"
    assert normalize_kraken_asset_code("ZABC", {"ABC", "EUR"}) == "ABC"
    assert normalize_kraken_asset_code("XABC", {"ABC2", "EUR"}) == "XABC"
    assert normalize_kraken_asset_code("XDG", {"XDG", "EUR"}) == "XDG"
    assert normalize_kraken_asset_code("XXDG", {"XDG", "EUR"}) == "XDG"


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


def test_inventory_uses_discovered_market_symbols_for_legacy_asset_codes():
    result = build_wallet_inventory(
        {"XXMR": "2"},
        {"XMR": 150},
        known_assets={"XMR", "EUR"},
    )
    assert result["assets"][0]["canonical_asset"] == "XMR"
    assert result["assets"][0]["value_eur"] == pytest.approx(300)


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
    assert [item["symbol"] for item in routes["EUR"]] == ["ETHEUR", "XBTEUR"]
    assert all(item["role"] == "quote" for item in routes["EUR"])


def test_pair_routes_normalize_other_legacy_assets_from_market_universe():
    routes = build_asset_pair_routes(
        ["XXMR"],
        [{"symbol": "XMREUR", "base": "XMR", "quote": "EUR"}],
    )
    assert [item["symbol"] for item in routes["XMR"]] == ["XMREUR"]

def test_toncoin_wallet_routes_include_kraken_eur_and_usd_spot_markets():
    routes = build_asset_pair_routes(
        ["TON"],
        [
            {"symbol": "TONEUR", "base": "TON", "quote": "EUR",
             "ordermin": "5", "costmin": "5"},
            {"symbol": "TONUSD", "base": "TON", "quote": "USD"},
            {"symbol": "BTCEUR", "base": "XBT", "quote": "EUR"},
        ],
    )
    assert [item["symbol"] for item in routes["TON"]] == ["TONEUR", "TONUSD"]
    assert routes["TON"][0]["ordermin"] == "5"
    assert routes["TON"][0]["costmin"] == "5"
