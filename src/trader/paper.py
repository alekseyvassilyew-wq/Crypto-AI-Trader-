import math

from .db import get_state, set_state


def _finite_number(value, name, *, minimum=None, strict_minimum=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None:
        invalid = number <= minimum if strict_minimum else number < minimum
        if invalid:
            op = "greater than" if strict_minimum else "at least"
            raise ValueError(f"{name} must be {op} {minimum}")
    return number


class PaperBroker:
    """Simulated spot wallet. No exchange/private account calls are made."""

    def __init__(self, balance=1000.0, fee_rate=0.001, db=None):
        self.db = db
        self.fee_rate = _finite_number(fee_rate, "fee_rate", minimum=0)
        if self.fee_rate >= 0.1:
            raise ValueError("fee_rate must be below 0.1")
        initial = _finite_number(balance, "balance", minimum=0)
        saved = get_state(db, "paper_broker") if db is not None else None
        self.positions = {}
        if saved:
            if not isinstance(saved, dict) or "cash" not in saved:
                raise ValueError("Invalid saved paper broker state")
            legacy_cash = _finite_number(saved["cash"], "saved cash", minimum=0)
            raw_cash = saved.get("cash_by_currency", {"EUR": legacy_cash})
            if not isinstance(raw_cash, dict):
                raise ValueError("Invalid saved currency balances")
            self.cash_by_currency = {
                str(k).upper(): _finite_number(v, f"{k} cash", minimum=0)
                for k, v in raw_cash.items()
            }
            self.cash_by_currency.setdefault("EUR", 0.0)
            raw_positions = saved.get("positions", {})
            if not isinstance(raw_positions, dict):
                raise ValueError("Invalid saved paper positions")
            for symbol, position in raw_positions.items():
                self._validate_symbol(symbol)
                if not isinstance(position, dict):
                    raise ValueError(f"Invalid saved position for {symbol}")
                self.positions[symbol] = {
                    "qty": _finite_number(position.get("qty"), f"{symbol} quantity", minimum=0, strict_minimum=True),
                    "avg_price": _finite_number(position.get("avg_price"), f"{symbol} average price", minimum=0, strict_minimum=True),
                    "quote_currency": str(position.get("quote_currency", "EUR")).upper(),
                }
        else:
            self.cash_by_currency = {"EUR": initial}
        self._sync_cash()
        self._persist()

    @staticmethod
    def _validate_symbol(symbol):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        return symbol.strip().upper()

    def _sync_cash(self):
        # Backward-compatible EUR property for existing commands/tests.
        self.cash = self.cash_by_currency.get("EUR", 0.0)

    def balance(self, currency="EUR"):
        return self.cash_by_currency.get(str(currency).upper(), 0.0)

    def _persist(self):
        if self.db is not None:
            set_state(self.db, "paper_broker", {
                "cash": self.cash_by_currency.get("EUR", 0.0),
                "cash_by_currency": self.cash_by_currency,
                "positions": self.positions,
            })

    def buy(self, symbol, quote, price, currency="EUR"):
        symbol = self._validate_symbol(symbol)
        currency = str(currency).upper()
        if not currency:
            raise ValueError("currency must not be empty")
        quote = _finite_number(quote, "quote", minimum=0, strict_minimum=True)
        price = _finite_number(price, "price", minimum=0, strict_minimum=True)
        fee = quote * self.fee_rate
        total_cost = quote + fee
        available = self.balance(currency)
        if not math.isfinite(total_cost) or total_cost > available + 1e-12:
            raise ValueError("Invalid paper buy or insufficient balance")
        qty = quote / price
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError("Paper buy quantity is invalid")
        old = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0, "quote_currency": currency})
        if old.get("quote_currency", currency) != currency:
            raise ValueError("Position quote currency mismatch")
        old_qty = _finite_number(old["qty"], "existing quantity", minimum=0)
        old_avg = _finite_number(old["avg_price"], "existing average price", minimum=0)
        total_qty = old_qty + qty
        average = ((old_qty * old_avg) + quote) / total_qty
        new_cash = available - total_cost
        if not all(math.isfinite(x) for x in (total_qty, average, new_cash)) or new_cash < -1e-9:
            raise ValueError("Paper buy would create an invalid account state")
        self.cash_by_currency[currency] = max(0.0, new_cash)
        self._sync_cash()
        self.positions[symbol] = {"qty": total_qty, "avg_price": average, "quote_currency": currency}
        self._persist()
        return {"symbol": symbol, "side": "Buy", "qty": qty, "quote": quote,
                "currency": currency, "fee": fee, "price": price, "status": "FILLED"}

    def sell(self, symbol, qty, price):
        symbol = self._validate_symbol(symbol)
        qty = _finite_number(qty, "qty", minimum=0, strict_minimum=True)
        price = _finite_number(price, "price", minimum=0, strict_minimum=True)
        pos = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0, "quote_currency": "EUR"})
        held = _finite_number(pos["qty"], "held quantity", minimum=0)
        if qty > held + 1e-12:
            raise ValueError("Invalid paper sell")
        currency = str(pos.get("quote_currency", "EUR")).upper()
        gross = qty * price
        fee = gross * self.fee_rate
        net = gross - fee
        remaining = max(0.0, held - qty)
        new_cash = self.balance(currency) + net
        if not all(math.isfinite(x) for x in (gross, fee, net, remaining, new_cash)) or net < 0:
            raise ValueError("Paper sell would create an invalid account state")
        if remaining < 1e-12:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol]["qty"] = remaining
        self.cash_by_currency[currency] = new_cash
        self._sync_cash()
        self._persist()
        return {"symbol": symbol, "side": "Sell", "qty": qty, "quote": gross,
                "currency": currency, "fee": fee, "price": price, "status": "FILLED"}

    def equity(self, prices, fx_rates=None):
        if not isinstance(prices, dict):
            raise ValueError("prices must be a dictionary")
        rates = {"EUR": 1.0}
        if fx_rates:
            rates.update({str(k).upper(): _finite_number(v, f"{k} FX rate", minimum=0, strict_minimum=True)
                          for k, v in fx_rates.items()})
        total = sum(amount * rates.get(currency, 0.0) for currency, amount in self.cash_by_currency.items())
        for symbol, position in self.positions.items():
            price = _finite_number(prices.get(symbol, position["avg_price"]),
                                   f"{symbol} mark price", minimum=0, strict_minimum=True)
            quote_currency = position.get("quote_currency", "EUR")
            total += position["qty"] * price * rates.get(quote_currency, 0.0)
        if not math.isfinite(total):
            raise ValueError("Paper equity is not finite")
        return total
