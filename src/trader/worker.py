"""Serialize blocking operations that share the trading engine and SQLite connection."""
import asyncio


class SerializedRunner:
    """Run blocking work off the event loop, one task at a time."""

    def __init__(self):
        self._lock = asyncio.Lock()

    async def run(self, fn):
        async with self._lock:
            return await asyncio.to_thread(fn)
