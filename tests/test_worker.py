import asyncio
import threading
import time

from src.trader.worker import SerializedRunner


def test_runner_serializes_shared_blocking_tasks():
    runner = SerializedRunner()
    guard = threading.Lock()
    state = {"active": 0, "peak": 0}

    def blocking_task(value):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with guard:
            state["active"] -= 1
        return value

    async def run_both():
        return await asyncio.gather(
            runner.run(lambda: blocking_task(1)),
            runner.run(lambda: blocking_task(2)),
        )

    assert asyncio.run(run_both()) == [1, 2]
    assert state["peak"] == 1


def test_runner_cancellation_keeps_lock_until_thread_finishes():
    runner = SerializedRunner()
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def first_task():
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        started.set()
        release.wait(timeout=2)
        with guard:
            state["active"] -= 1
        return "first"

    def second_task():
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        second_started.set()
        with guard:
            state["active"] -= 1
        return "second"

    async def scenario():
        first = asyncio.create_task(runner.run(first_task))
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        second = asyncio.create_task(runner.run(second_task))
        await asyncio.sleep(0.05)
        assert not second_started.is_set()
        release.set()
        try:
            await first
        except asyncio.CancelledError:
            pass
        assert await second == "second"

    asyncio.run(scenario())
    assert state["peak"] == 1
    assert second_started.is_set()
