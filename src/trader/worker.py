"""Serialize blocking operations that share the trading engine and SQLite connection."""
import asyncio


class SerializedRunner:
    """Run blocking work off the event loop, one task at a time.

    Cancelling the awaiting coroutine must not release the serialization lock
    while its worker thread is still using shared engine or SQLite state.
    """

    def __init__(self):
        self._lock = asyncio.Lock()

    async def run(self, fn):
        async with self._lock:
            worker = asyncio.create_task(asyncio.to_thread(fn))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # asyncio.to_thread cannot stop a running thread. Keep the lock
                # until that thread has really finished, otherwise a subsequent
                # operation could concurrently touch the same engine/connection.
                try:
                    await asyncio.shield(worker)
                except BaseException:
                    # Preserve cancellation of the caller; worker exceptions
                    # are observed here so they do not become unhandled tasks.
                    pass
                raise
