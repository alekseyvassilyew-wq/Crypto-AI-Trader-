import pytest

from src.trader.wallet import build_wallet_inventory


def test_empty_wallet_is_a_valid_zero_balance():
    result = build_wallet_inventory({}, {"EUR": 1.0})
    assert result == {
        "assets": [],
        "asset_count": 0,
        "known_total_eur": 0,
        "unknown_assets": [],
        "total_complete": True,
        "total_eur": 0,
    }


def test_multiple_assets_and_tiny_positive_residuals_are_preserved():
    result = build_wallet_inventory(
        {"ZEUR": "12.50", "XXBT": "0.00000001", "ZUSD": "2"},
        {"ZEUR": 1.0, "XXBT": 60000.0, "ZUSD": 0.92},
    )
    by_asset = {item["asset"]: item for item in result["assets"]}
    assert result["asset_count"] == 3
    assert by_asset["XXBT"]["amount"] == pytest.approx(0.00000001)
    assert by_asset["XXBT"]["value_eur"] == pytest.approx(0.0006)
    assert result["total_complete"] is True
    assert result["total_eur"] == pytest.approx(14.3406)


def test_unknown_fx_is_not_misreported_as_zero_value():
    result = build_wallet_inventory({"SOMECOIN": "3.5"}, {"EUR": 1.0})
    assert result["assets"][0]["value_eur"] is None
    assert result["unknown_assets"] == ["SOMECOIN"]
    assert result["total_complete"] is False
    assert result["total_eur"] is None
    assert result["known_total_eur"] == 0


def test_invalid_and_zero_amounts_are_ignored_but_negative_are_not_listed():
    result = build_wallet_inventory(
        {"ZERO": "0", "NEG": "-1", "BAD": "NaN", "TEXT": "oops", "BTC": "0.1"},
        {"BTC": 50000},
    )
    assert [item["asset"] for item in result["assets"]] == ["BTC"]


def test_invalid_fx_rates_are_ignored():
    result = build_wallet_inventory({"COIN": "2"}, {"COIN": "inf"})
    assert result["assets"][0]["value_eur"] is None


def test_balance_payload_must_be_a_mapping():
    with pytest.raises(ValueError):
        build_wallet_inventory([], {})
