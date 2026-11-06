import pytest
from src.trader.paper import PaperBroker

def test_paper_fees_average_entry_and_fills():
    b = PaperBroker(100, .001)
    x = b.buy("BTCUSDT", 20, 10)
    assert x["qty"] == 2
    assert b.cash == pytest.approx(79.98)
    b.buy("BTCUSDT", 20, 20)
    assert b.positions["BTCUSDT"]["qty"] == pytest.approx(3)
    assert b.positions["BTCUSDT"]["avg_price"] == pytest.approx(40/3)
    b.sell("BTCUSDT", 1, 12)
    assert b.cash == pytest.approx(71.948)

def test_no_overspend():
    with pytest.raises(ValueError): PaperBroker(10).buy("BTCUSDT", 11, 10)

def test_equity_marks_positions():
    b = PaperBroker(100)
    b.buy("BTCUSDT", 20, 10)
    assert b.equity({"BTCUSDT": 12}) == pytest.approx(103.98)


def test_buy_and_sell_preserve_cash_and_position():
    broker = PaperBroker(balance=20, fee_rate=0.01)
    bought = broker.buy("XBTEUR", 10, 2)
    assert bought["qty"] == 5
    assert broker.cash == pytest.approx(9.9)
    sold = broker.sell("XBTEUR", 2, 3)
    assert sold["quote"] == 6
    assert broker.cash == pytest.approx(15.84)
    assert broker.positions["XBTEUR"]["qty"] == pytest.approx(3)


@pytest.mark.parametrize("balance,fee_rate", [
    (float("nan"), 0.001), (float("inf"), 0.001), (-1, 0.001),
    (10, float("nan")), (10, -0.1), (10, 0.1),
])
def test_constructor_rejects_invalid_account_settings(balance, fee_rate):
    with pytest.raises(ValueError):
        PaperBroker(balance=balance, fee_rate=fee_rate)


@pytest.mark.parametrize("quote,price", [
    (float("nan"), 1), (1, float("inf")), (-1, 1), (1, 0),
])
def test_buy_rejects_invalid_amounts_without_mutating_account(quote, price):
    broker = PaperBroker(balance=20)
    before_cash = broker.cash
    with pytest.raises(ValueError):
        broker.buy("XBTEUR", quote, price)
    assert broker.cash == before_cash
    assert broker.positions == {}


def test_sell_rejects_more_than_held_without_mutating_account():
    broker = PaperBroker(balance=20)
    broker.buy("XBTEUR", 5, 10)
    before_cash = broker.cash
    before_position = dict(broker.positions["XBTEUR"])
    with pytest.raises(ValueError):
        broker.sell("XBTEUR", 1, 10)
    assert broker.cash == before_cash
    assert broker.positions["XBTEUR"] == before_position


def test_equity_rejects_non_finite_mark_price():
    broker = PaperBroker(balance=20)
    broker.buy("XBTEUR", 5, 10)
    with pytest.raises(ValueError):
        broker.equity({"XBTEUR": float("nan")})


def test_load_rejects_corrupted_persisted_position(tmp_path):
    import json
    from src.trader.db import connect
    db = connect(tmp_path / "corrupt.sqlite3")
    # Simulate legacy/corrupt data written outside the validated state helper.
    payload = json.dumps({
        "cash": 10,
        "positions": {"XBTEUR": {"qty": float("nan"), "avg_price": 10}},
    })
    db.execute("INSERT INTO app_state(key, value) VALUES(?, ?)", ("paper_broker", payload))
    db.commit()
    with pytest.raises(ValueError):
        PaperBroker(db=db)
    db.close()


def test_multi_currency_wallet_debits_and_credits_matching_quote_currency():
    broker = PaperBroker(balance=20, fee_rate=0.01)
    broker.cash_by_currency["USD"] = 50
    bought = broker.buy("ETHUSD", 20, 10, currency="USD")
    assert bought["currency"] == "USD"
    assert broker.balance("USD") == pytest.approx(29.8)
    assert broker.cash == pytest.approx(20)
    sold = broker.sell("ETHUSD", 1, 12)
    assert sold["currency"] == "USD"
    assert broker.balance("USD") == pytest.approx(41.68)
    assert broker.cash == pytest.approx(20)

 
 
def test_paper_currency_conversion_moves_virtual_cash_and_charges_fee():
    broker = PaperBroker(balance=20, fee_rate=0.01)
    converted = broker.convert("EUR", "USD", 10, 1.1)
    assert converted["spent"] == pytest.approx(10)
    assert converted["received"] == pytest.approx(10.89)
    assert broker.balance("EUR") == pytest.approx(10)
    assert broker.balance("USD") == pytest.approx(10.89)


def test_multi_currency_equity_uses_fx_rates():
    broker = PaperBroker(balance=20)
    broker.cash_by_currency["USD"] = 10
    assert broker.equity({}, {"USD": 0.9}) == pytest.approx(29)

def test_trailing_peak_is_persisted_across_broker_restart(tmp_path):
    from src.trader.db import connect
    db = connect(tmp_path / "trailing.sqlite3")
    broker = PaperBroker(balance=20, fee_rate=0.001, db=db)
    broker.buy("XBTEUR", 5, 10)
    assert broker.update_peak("XBTEUR", 12) == 12
    restored = PaperBroker(balance=20, fee_rate=0.001, db=db)
    assert restored.positions["XBTEUR"]["peak_price"] == 12
    assert restored.update_peak("XBTEUR", 11) == 12
    db.close()


def test_empty_corrupt_persisted_wallet_fails_closed(tmp_path):
    from src.trader.db import connect, set_state
    db = connect(tmp_path / "empty-wallet.sqlite3")
    set_state(db, "paper_broker", {})
    with pytest.raises(ValueError, match="Invalid saved paper broker state"):
        PaperBroker(balance=20, db=db)
    db.close()


def test_duplicate_saved_currency_codes_fail_closed(tmp_path):
    from src.trader.db import connect, set_state
    db = connect(tmp_path / "duplicate-currency.sqlite3")
    set_state(db, "paper_broker", {
        "cash": 20,
        "cash_by_currency": {"usd": 5, "USD": 6},
        "positions": {},
    })
    with pytest.raises(ValueError, match="Duplicate saved currency"):
        PaperBroker(balance=20, db=db)
    db.close()


def test_malformed_persisted_wallet_json_fails_closed(tmp_path):
    from src.trader.db import connect
    db = connect(tmp_path / "malformed-wallet.sqlite3")
    db.execute("INSERT INTO app_state(key, value) VALUES(?, ?)", ("paper_broker", "{broken"))
    db.commit()
    with pytest.raises(ValueError, match="invalid JSON"):
        PaperBroker(balance=20, db=db)
    db.close()


def test_non_object_persisted_wallet_json_fails_closed(tmp_path):
    from src.trader.db import connect
    db = connect(tmp_path / "null-wallet.sqlite3")
    db.execute("INSERT INTO app_state(key, value) VALUES(?, ?)", ("paper_broker", "null"))
    db.commit()
    with pytest.raises(ValueError, match="JSON object"):
        PaperBroker(balance=20, db=db)
    db.close()


def test_conversion_rejects_target_balance_overflow():
    broker = PaperBroker(balance=1.0, fee_rate=0.0)
    broker.cash_by_currency["EUR"] = 1.0
    broker.cash_by_currency["USD"] = 1e308
    with pytest.raises(ValueError, match="invalid wallet balance"):
        broker.convert("EUR", "USD", 1.0, 1e308)



def test_eur_cost_basis_and_entry_fees_follow_partial_sells():
    broker = PaperBroker(balance=100, fee_rate=0.01)
    broker.cash_by_currency["USD"] = 50
    broker._persist()
    broker.buy("ETHUSD", 20, 10, currency="USD", quote_to_eur_rate=0.9)
    position = broker.positions["ETHUSD"]
    assert position["cost_basis_eur"] == pytest.approx(18.0)
    assert position["entry_fees_eur"] == pytest.approx(0.18)
    broker.sell("ETHUSD", 1, 12)
    remaining = broker.positions["ETHUSD"]
    assert remaining["qty"] == pytest.approx(1.0)
    assert remaining["cost_basis_eur"] == pytest.approx(9.0)
    assert remaining["entry_fees_eur"] == pytest.approx(0.09)



def test_eur_cost_basis_survives_restart_and_partial_exit(tmp_path):
    from src.trader.db import connect

    db = connect(tmp_path / "eur-basis.sqlite3")
    broker = PaperBroker(balance=100, fee_rate=0.01, db=db)
    broker.cash_by_currency["USD"] = 50
    broker._persist()
    broker.buy("ETHUSD", 20, 10, currency="USD", quote_to_eur_rate=0.9)
    restored = PaperBroker(balance=100, fee_rate=0.01, db=db)
    assert restored.positions["ETHUSD"]["cost_basis_eur"] == pytest.approx(18.0)
    assert restored.positions["ETHUSD"]["entry_fees_eur"] == pytest.approx(0.18)
    restored.sell("ETHUSD", 1, 12)
    restarted_again = PaperBroker(balance=100, fee_rate=0.01, db=db)
    assert restarted_again.positions["ETHUSD"]["qty"] == pytest.approx(1.0)
    assert restarted_again.positions["ETHUSD"]["cost_basis_eur"] == pytest.approx(9.0)
    assert restarted_again.positions["ETHUSD"]["entry_fees_eur"] == pytest.approx(0.09)
    db.close()



def test_convert_can_defer_persistence(tmp_path):
    from src.trader.db import connect, get_state

    db = connect(tmp_path / "deferred-conversion.sqlite3")
    broker = PaperBroker(balance=20, fee_rate=0.01, db=db)
    persisted_before = get_state(db, "paper_broker")
    broker.convert("EUR", "USD", 10, 1.1, persist=False)
    assert broker.balance("EUR") == pytest.approx(10)
    assert broker.balance("USD") == pytest.approx(10.89)
    assert get_state(db, "paper_broker") == persisted_before
    broker._persist()
    assert get_state(db, "paper_broker")["cash_by_currency"]["USD"] == pytest.approx(10.89)
    db.close()


def test_convert_route_rolls_back_all_legs_when_later_leg_fails(tmp_path):
    from src.trader.db import connect, get_state

    db = connect(tmp_path / "atomic-conversion.sqlite3")
    broker = PaperBroker(balance=20, fee_rate=0.01, db=db)
    before = get_state(db, "paper_broker")
    with pytest.raises(ValueError, match="conversion rate"):
        broker.convert_route([
            ("EUR", "USD", 10, 1.1),
            ("USD", "GBP", 5, 0),
        ])
    assert broker.cash_by_currency == {"EUR": 20.0}
    assert broker.cash == pytest.approx(20)
    assert get_state(db, "paper_broker") == before
    db.close()


def test_convert_route_persists_successful_legs_together(tmp_path):
    from src.trader.db import connect, get_state

    db = connect(tmp_path / "atomic-conversion-success.sqlite3")
    broker = PaperBroker(balance=20, fee_rate=0.01, db=db)
    first_received = 10 * 1.1 * (1 - broker.fee_rate)
    first, second = broker.convert_route([
        ("EUR", "USD", 10, 1.1),
        ("USD", "GBP", first_received, 0.8),
    ])
    saved = get_state(db, "paper_broker")
    assert first["received"] == pytest.approx(first_received)
    assert second["received"] == pytest.approx(first_received * 0.8 * (1 - broker.fee_rate))
    assert saved["cash_by_currency"]["EUR"] == pytest.approx(10)
    assert saved["cash_by_currency"]["USD"] == pytest.approx(0)
    assert saved["cash_by_currency"]["GBP"] == pytest.approx(second["received"])
    db.close()


def test_multiple_buys_accumulate_eur_basis_and_partial_sell_allocates_proportionally():
    broker = PaperBroker(balance=200, fee_rate=0.01)
    broker.cash_by_currency["USD"] = 100
    broker._persist()

    broker.buy("ETHEUR", 20, 10, currency="USD", quote_to_eur_rate=0.90)
    broker.buy("ETHEUR", 30, 15, currency="USD", quote_to_eur_rate=0.95)

    position = broker.positions["ETHEUR"]
    assert position["qty"] == pytest.approx(20 / 10 + 30 / 15)
    # The second buy consumes the remaining inventory at its weighted-average
    # historical EUR basis; a new spot FX mark must not revalue existing cash.
    assert position["cost_basis_eur"] == pytest.approx(50 * 0.90)
    assert position["entry_fees_eur"] == pytest.approx(50 * 0.01 * 0.90)

    broker.sell("ETHEUR", 1.0, 16)
    remaining = broker.positions["ETHEUR"]
    assert remaining["qty"] == pytest.approx(3.0)
    assert remaining["cost_basis_eur"] == pytest.approx((50 * 0.90) * 3 / 4)
    assert remaining["entry_fees_eur"] == pytest.approx((50 * 0.01 * 0.90) * 3 / 4)


def test_equity_fails_closed_when_non_eur_cash_has_no_fx_rate():
    broker = PaperBroker(balance=20)
    broker.cash_by_currency["USD"] = 10
    with pytest.raises(ValueError, match="Missing FX valuation rate.*USD"):
        broker.equity({}, {})


def test_equity_fails_closed_when_open_position_mark_is_missing():
    broker = PaperBroker(balance=20)
    broker.buy("XBTEUR", 5, 10)
    with pytest.raises(ValueError, match="Missing current PAPER mark price.*XBTEUR"):
        broker.equity({}, {"EUR": 1.0})


def test_equity_fails_closed_when_position_quote_fx_is_missing():
    broker = PaperBroker(balance=20)
    broker.cash_by_currency["USD"] = 10
    broker.buy("ETHUSD", 5, 10, currency="USD")
    with pytest.raises(ValueError, match="Missing FX valuation rate.*USD"):
        broker.equity({"ETHUSD": 11}, {"EUR": 1.0})


def test_estimate_book_vwap_consumes_multiple_ask_levels():
    from src.trader.paper import estimate_book_vwap

    result = estimate_book_vwap([[10, 1], [11, 2], [12, 5]], 2, side="buy")
    assert result["qty"] == pytest.approx(2)
    assert result["quote"] == pytest.approx(21)
    assert result["vwap"] == pytest.approx(10.5)


def test_estimate_book_vwap_consumes_multiple_bid_levels():
    from src.trader.paper import estimate_book_vwap

    result = estimate_book_vwap([[12, 1], [11, 2], [10, 5]], 2, side="sell")
    assert result["quote"] == pytest.approx(23)
    assert result["vwap"] == pytest.approx(11.5)


def test_estimate_book_vwap_fails_closed_when_depth_is_insufficient():
    from src.trader.paper import estimate_book_vwap

    with pytest.raises(ValueError, match="insufficient visible"):
        estimate_book_vwap([[10, 0.5]], 1, side="buy")


def test_estimate_book_vwap_rejects_unsorted_or_invalid_levels():
    from src.trader.paper import estimate_book_vwap

    with pytest.raises(ValueError, match="not sorted"):
        estimate_book_vwap([[10, 1], [9, 1]], 1.5, side="buy")
    with pytest.raises(ValueError):
        estimate_book_vwap([[10, 0]], 1, side="buy")

def test_fx_conversion_basis_flows_into_asset_cost_and_partial_sale_proceeds(tmp_path):
    from src.trader.db import connect
    db = connect(tmp_path / "cash-cost-basis.sqlite3")
    broker = PaperBroker(balance=100, fee_rate=0.01, db=db)

    conversion = broker.convert("EUR", "USD", 50, 2.0)
    assert conversion["received"] == pytest.approx(99.0)
    assert broker.cash_cost_basis_eur["EUR"] == pytest.approx(50.0)
    assert broker.cash_cost_basis_eur["USD"] == pytest.approx(50.0)

    broker.buy("ETHUSD", 49.5, 10, currency="USD")
    position = broker.positions["ETHUSD"]
    assert position["cost_basis_eur"] == pytest.approx(25.0)
    assert position["entry_fees_eur"] == pytest.approx(0.25)
    assert broker.cash_cost_basis_eur["USD"] == pytest.approx(24.75)

    restored = PaperBroker(balance=100, fee_rate=0.01, db=db)
    assert restored.cash_cost_basis_eur["USD"] == pytest.approx(24.75)
    assert restored.positions["ETHUSD"]["cost_basis_eur"] == pytest.approx(25.0)

    restored.sell("ETHUSD", 2.0, 12.0, quote_to_eur_rate=0.5)
    remaining = restored.positions["ETHUSD"]
    assert remaining["cost_basis_eur"] == pytest.approx(25.0 * (1 - 2.0 / 4.95))
    assert remaining["entry_fees_eur"] == pytest.approx(0.25 * (1 - 2.0 / 4.95))
    assert restored.cash_cost_basis_eur["USD"] == pytest.approx(
        24.75 + (24.0 - 0.24) * 0.5
    )
    restarted = PaperBroker(balance=100, fee_rate=0.01, db=db)
    assert restarted.cash_cost_basis_eur["USD"] == pytest.approx(
        24.75 + (24.0 - 0.24) * 0.5
    )
    db.close()


def test_legacy_non_eur_cash_basis_remains_unknown_after_restart(tmp_path):
    from src.trader.db import connect, set_state, get_state
    db = connect(tmp_path / "legacy-cash-basis.sqlite3")
    set_state(db, "paper_broker", {
        "cash": 20.0,
        "cash_by_currency": {"EUR": 20.0, "USD": 50.0},
        "positions": {},
    })
    broker = PaperBroker(balance=20, fee_rate=0.01, db=db)
    assert broker.cash_cost_basis_eur["EUR"] == pytest.approx(20.0)
    assert broker.cash_cost_basis_eur["USD"] is None

    # A fresh spot mark must not fabricate the acquisition cost of legacy USD.
    broker.buy("ETHUSD", 10.0, 2.0, currency="USD", quote_to_eur_rate=0.9)
    assert broker.positions["ETHUSD"]["cost_basis_eur"] is None
    assert broker.positions["ETHUSD"]["entry_fees_eur"] is None
    restored = PaperBroker(balance=20, fee_rate=0.01, db=db)
    assert restored.cash_cost_basis_eur["USD"] is None
    assert restored.positions["ETHUSD"]["cost_basis_eur"] is None
    assert get_state(db, "paper_broker")["cash_cost_basis_eur"]["USD"] is None
    db.close()


def test_conversion_route_carries_unknown_basis_without_inventing_value():
    broker = PaperBroker(balance=20, fee_rate=0.01)
    broker.cash_by_currency["USD"] = 50.0
    broker.cash_cost_basis_eur["USD"] = None
    first, second = broker.convert_route([
        ("USD", "EUR", 10.0, 0.9),
        ("EUR", "GBP", 8.91, 0.8),
    ])
    assert first["received"] == pytest.approx(8.91)
    assert second["received"] == pytest.approx(7.12872)
    assert broker.cash_cost_basis_eur["GBP"] is None

