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
        self.restored_from_db = saved is not None
        self.positions = {}
        if saved is not None:
            if not isinstance(saved, dict) or "cash" not in saved:
                raise ValueError("Invalid saved paper broker state")
            legacy_cash = _finite_number(saved["cash"], "saved cash", minimum=0)
            raw_cash = saved.get("cash_by_currency", {"EUR": legacy_cash})
            if not isinstance(raw_cash, dict):
                raise ValueError("Invalid saved currency balances")
            self.cash_by_currency = {}
            for raw_currency, raw_amount in raw_cash.items():
                currency = self._validate_currency(raw_currency)
                if currency in self.cash_by_currency:
                    raise ValueError(f"Duplicate saved currency balance for {currency}")
                self.cash_by_currency[currency] = _finite_number(
                    raw_amount, f"{currency} cash", minimum=0
                )
            self.cash_by_currency.setdefault("EUR", 0.0)
            raw_positions = saved.get("positions", {})
            if not isinstance(raw_positions, dict):
                raise ValueError("Invalid saved paper positions")
            for symbol, position in raw_positions.items():
                symbol = self._validate_symbol(symbol)
                if symbol in self.positions:
                    raise ValueError(f"Duplicate saved position for {symbol}")
                if not isinstance(position, dict):
                    raise ValueError(f"Invalid saved position for {symbol}")
                self.positions[symbol] = {
                    "qty": _finite_number(position.get("qty"), f"{symbol} quantity", minimum=0, strict_minimum=True),
                    "avg_price": _finite_number(position.get("avg_price"), f"{symbol} average price", minimum=0, strict_minimum=True),
                    "quote_currency": self._validate_currency(position.get("quote_currency", "EUR")),
                    "peak_price": _finite_number(position.get("peak_price", position.get("avg_price")), f"{symbol} peak price", minimum=0, strict_minimum=True),
                    "cost_basis_eur": None if position.get("cost_basis_eur") is None else _finite_number(position["cost_basis_eur"], f"{symbol} EUR cost basis", minimum=0),
                    "entry_fees_eur": None if position.get("entry_fees_eur") is None else _finite_number(position["entry_fees_eur"], f"{symbol} EUR entry fees", minimum=0),
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

    @staticmethod
    def _validate_currency(currency):
        if not isinstance(currency, str):
            raise ValueError("currency must be a non-empty alphanumeric code")
        currency = currency.strip().upper()
        if not currency or not currency.isalnum() or len(currency) > 12:
            raise ValueError("currency must be a non-empty alphanumeric code")
        return currency

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

    def convert(self, from_currency, to_currency, amount, rate, *, persist=True):
        """Convert virtual cash only; rate is destination units per source unit."""
        source = self._validate_currency(from_currency)
        target = self._validate_currency(to_currency)
        if not source or not target or source == target:
            raise ValueError("Currency conversion requires two different currencies")
        amount = _finite_number(amount, "conversion amount", minimum=0, strict_minimum=True)
        rate = _finite_number(rate, "conversion rate", minimum=0, strict_minimum=True)
        if amount > self.balance(source) + 1e-12:
            raise ValueError("Insufficient PAPER balance for currency conversion")
        gross = amount * rate
        fee = gross * self.fee_rate
        received = gross - fee
        if not all(math.isfinite(x) for x in (gross, fee, received)) or received <= 0:
            raise ValueError("Invalid PAPER currency conversion")
        new_source_balance = self.balance(source) - amount
        new_target_balance = self.balance(target) + received
        if not math.isfinite(new_source_balance) or not math.isfinite(new_target_balance) or new_source_balance < -1e-9:
            raise ValueError("PAPER conversion would create an invalid wallet balance")
        self.cash_by_currency[source] = max(0.0, new_source_balance)
        self.cash_by_currency[target] = new_target_balance
        self._sync_cash()
        if persist:
            self._persist()
        return {"from": source, "to": target, "spent": amount, "received": received,
                "fee": fee, "rate": rate}

    def convert_route(self, legs):
        """Apply a multi-leg virtual-cash conversion as one persisted wallet update.

        Each leg is (from_currency, to_currency, amount, rate). If any leg
        or the final persistence fails, restore the in-memory wallet and leave
        the persisted wallet at its pre-route state.
        """
        cash_before = dict(self.cash_by_currency)
        try:
            conversions = [
                self.convert(source, target, amount, rate, persist=False)
                for source, target, amount, rate in legs
            ]
            self._persist()
            return conversions
        except Exception:
            self.cash_by_currency = cash_before
            self._sync_cash()
            self._persist()
            raise

    def buy(self, symbol, quote, price, currency="EUR", quote_to_eur_rate=None, *, persist=True):
        symbol = self._validate_symbol(symbol)
        currency = self._validate_currency(currency)
        quote = _finite_number(quote, "quote", minimum=0, strict_minimum=True)
        price = _finite_number(price, "price", minimum=0, strict_minimum=True)
        rate = None if quote_to_eur_rate is None else _finite_number(
            quote_to_eur_rate, "quote_to_eur_rate", minimum=0, strict_minimum=True
        )
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
        old_basis = old.get("cost_basis_eur")
        old_fees = old.get("entry_fees_eur")
        cost_basis_eur = None
        entry_fees_eur = None
        if rate is not None and (old_qty == 0 or old_basis is not None):
            cost_basis_eur = (float(old_basis or 0.0) + quote * rate)
        if rate is not None and (old_qty == 0 or old_fees is not None):
            entry_fees_eur = (float(old_fees or 0.0) + fee * rate)
        self.cash_by_currency[currency] = max(0.0, new_cash)
        self._sync_cash()
        peak_price = max(float(old.get("peak_price", old_avg) or old_avg), price)
        self.positions[symbol] = {
            "qty": total_qty, "avg_price": average, "quote_currency": currency,
            "peak_price": peak_price, "cost_basis_eur": cost_basis_eur,
            "entry_fees_eur": entry_fees_eur,
        }
        if persist:
            self._persist()
        return {"symbol": symbol, "side": "Buy", "qty": qty, "quote": quote,
                "currency": currency, "fee": fee, "price": price, "status": "FILLED"}

    def update_peak(self, symbol, price):
        """Persist the highest observed mark for a position's trailing stop."""
        symbol = self._validate_symbol(symbol)
        if symbol not in self.positions:
            return None
        price = _finite_number(price, "mark price", minimum=0, strict_minimum=True)
        position = self.positions[symbol]
        peak = max(float(position.get("peak_price", position["avg_price"])), price)
        if peak != position.get("peak_price"):
            position["peak_price"] = peak
            self._persist()
        return peak

    def sell(self, symbol, qty, price, *, persist=True):
        symbol = self._validate_symbol(symbol)
        qty = _finite_number(qty, "qty", minimum=0, strict_minimum=True)
        price = _finite_number(price, "price", minimum=0, strict_minimum=True)
        pos = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0, "quote_currency": "EUR"})
        held = _finite_number(pos["qty"], "held quantity", minimum=0)
        if qty > held + 1e-12:
            raise ValueError("Invalid paper sell")
        currency = self._validate_currency(pos.get("quote_currency", "EUR"))
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
            remaining_ratio = remaining / held
            self.positions[symbol]["qty"] = remaining
            for field in ("cost_basis_eur", "entry_fees_eur"):
                value = self.positions[symbol].get(field)
                if value is not None:
                    self.positions[symbol][field] = float(value) * remaining_ratio
        self.cash_by_currency[currency] = new_cash
        self._sync_cash()
        if persist:
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
