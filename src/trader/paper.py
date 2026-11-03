from .db import get_state, set_state

class PaperBroker:
    def __init__(self, balance=1000.0, fee_rate=0.001, db=None):
        self.fee_rate, self.db = float(fee_rate), db
        saved = get_state(db, "paper_broker") if db is not None else None
        if saved:
            self.cash = float(saved["cash"])
            self.positions = {k: dict(v) for k, v in saved.get("positions", {}).items()}
        else:
            self.cash, self.positions = float(balance), {}
            self._persist()

    def _persist(self):
        if self.db is not None:
            set_state(self.db, "paper_broker", {"cash": self.cash, "positions": self.positions})

    def buy(self, symbol, quote, price):
        quote, price = float(quote), float(price)
        fee = quote * self.fee_rate
        if quote <= 0 or price <= 0 or quote + fee > self.cash:
            raise ValueError("Invalid paper buy or insufficient balance")
        qty = quote / price
        old = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
        total = old["qty"] + qty
        avg = ((old["qty"] * old["avg_price"]) + quote) / total
        self.cash -= quote + fee
        self.positions[symbol] = {"qty": total, "avg_price": avg}
        self._persist()
        return {"symbol": symbol, "side": "Buy", "qty": qty, "quote": quote, "fee": fee, "price": price, "status": "FILLED"}

    def sell(self, symbol, qty, price):
        qty, price = float(qty), float(price)
        pos = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
        held = float(pos["qty"])
        if qty <= 0 or qty > held + 1e-12 or price <= 0:
            raise ValueError("Invalid paper sell")
        gross, fee = qty * price, qty * price * self.fee_rate
        remaining = max(0.0, held - qty)
        if remaining < 1e-12: self.positions.pop(symbol, None)
        else: self.positions[symbol]["qty"] = remaining
        self.cash += gross - fee
        self._persist()
        return {"symbol": symbol, "side": "Sell", "qty": qty, "quote": gross, "fee": fee, "price": price, "status": "FILLED"}

    def equity(self, prices):
        return self.cash + sum(p["qty"] * float(prices.get(sym, p["avg_price"])) for sym, p in self.positions.items())
