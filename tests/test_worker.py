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
