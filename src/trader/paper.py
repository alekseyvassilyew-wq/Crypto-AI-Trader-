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
    def __init__(self, balance=1000.0, fee_rate=0.001, db=None):
        self.db = db
        self.fee_rate = _finite_number(fee_rate, "fee_rate", minimum=0)
        if self.fee_rate >= 0.1:
            raise ValueError("fee_rate must be below 0.1")
        initial_balance = _finite_number(balance, "balance", minimum=0)

        saved = get_state(db, "paper_broker") if db is not None else None
        if saved:
            if not isinstance(saved, dict) or "cash" not in saved:
                raise ValueError("Invalid saved paper broker state")
            self.cash = _finite_number(saved["cash"], "saved cash", minimum=0)
            raw_positions = saved.get("positions", {})
            if not isinstance(raw_positions, dict):
                raise ValueError("Invalid saved paper positions")
            self.positions = {}
            for symbol, position in raw_positions.items():
                self._validate_symbol(symbol)
                if not isinstance(position, dict):
                    raise ValueError(f"Invalid saved position for {symbol}")
                qty = _finite_number(position.get("qty"), f"{symbol} quantity", minimum=0, strict_minimum=True)
                avg_price = _finite_number(position.get("avg_price"), f"{symbol} average price", minimum=0, strict_minimum=True)
                self.positions[symbol] = {"qty": qty, "avg_price": avg_price}
        else:
            self.cash, self.positions = initial_balance, {}
            self._persist()

    @staticmethod
    def _validate_symbol(symbol):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        return symbol.strip().upper()

    def _persist(self):
        if self.db is not None:
            set_state(self.db, "paper_broker", {"cash": self.cash, "positions": self.positions})

    def balance(self, currency="EUR"):
        """Return free PAPER cash in the requested quote currency."""
        currency = str(currency).upper()
        return self.cash if currency == "EUR" else self.cash_by_currency.get(currency, 0.0)

    def buy(self, symbol, quote, price):
        symbol = self._validate_symbol(symbol)
        quote = _finite_number(quote, "quote", minimum=0, strict_minimum=True)
        price = _finite_number(price, "price", minimum=0, strict_minimum=True)
        fee = quote * self.fee_rate
        total_cost = quote + fee
        if not math.isfinite(total_cost) or total_cost > self.cash + 1e-12:
            raise ValueError("Invalid paper buy or insufficient balance")
        qty = quote / price
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError("Paper buy quantity is invalid")

        old = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
        old_qty = _finite_number(old["qty"], "existing quantity", minimum=0)
        old_avg = _finite_number(old["avg_price"], "existing average price", minimum=0)
        total_qty = old_qty + qty
        average = ((old_qty * old_avg) + quote) / total_qty
        new_cash = self.cash - total_cost
        if not all(math.isfinite(x) for x in (total_qty, average, new_cash)) or new_cash < -1e-9:
            raise ValueError("Paper buy would create an invalid account state")

        self.cash = max(0.0, new_cash)
        self.positions[symbol] = {"qty": total_qty, "avg_price": average}
        self._persist()
        return {"symbol": symbol, "side": "Buy", "qty": qty, "quote": quote,
                "fee": fee, "price": price, "status": "FILLED"}

    def sell(self, symbol, qty, price):
        symbol = self._validate_symbol(symbol)
        qty = _finite_number(qty, "qty", minimum=0, strict_minimum=True)
        price = _finite_number(price, "price", minimum=0, strict_minimum=True)
        pos = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
        held = _finite_number(pos["qty"], "held quantity", minimum=0)
        if qty > held + 1e-12:
            raise ValueError("Invalid paper sell")
        gross = qty * price
        fee = gross * self.fee_rate
        net = gross - fee
        remaining = max(0.0, held - qty)
        new_cash = self.cash + net
        if not all(math.isfinite(x) for x in (gross, fee, net, remaining, new_cash)) or net < 0:
            raise ValueError("Paper sell would create an invalid account state")

        if remaining < 1e-12:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol]["qty"] = remaining
        self.cash = new_cash
        self._persist()
        return {"symbol": symbol, "side": "Sell", "qty": qty, "quote": gross,
                "fee": fee, "price": price, "status": "FILLED"}

    def equity(self, prices):
        if not isinstance(prices, dict):
            raise ValueError("prices must be a dictionary")
        total = self.cash
        for symbol, position in self.positions.items():
            price = prices.get(symbol, position["avg_price"])
            price = _finite_number(price, f"{symbol} mark price", minimum=0, strict_minimum=True)
            total += position["qty"] * price
        if not math.isfinite(total):
            raise ValueError("Paper equity is not finite")
        return total
