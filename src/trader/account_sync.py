"""Explicit, read-only Kraken account synchronization."""
from datetime import datetime, timezone

from .db import import_kraken_account_history


def sync_kraken_account(exchange, db, *, fetched_at=None):
    """Fetch balances, all available fills and ledger entries, then persist atomically.

    All three reads must succeed before any snapshot is written. The exchange
    adapter rejects incomplete paginated history. No order or account mutation
    methods are called.
    """
    timestamp = fetched_at or datetime.now(timezone.utc).isoformat()
    balances = exchange.balance()
    trades = exchange.trades_history()
    ledgers = exchange.ledger_history()
    return import_kraken_account_history(
        db,
        trades=trades,
        ledgers=ledgers,
        balances=balances,
        fetched_at=timestamp,
        fills_complete=True,
        ledger_complete=True,
    )
