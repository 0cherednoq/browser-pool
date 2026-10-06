"""Виртуальное время: часы и таймауты пула проверяются без ожидания и детерминированно."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from browser_pool import clock
from browser_pool.testing import VirtualTimeLoop


async def test_long_sleep_takes_no_real_time() -> None:
    started_real = time.perf_counter()
    started = clock.monotonic()

    await asyncio.sleep(3600)

    assert clock.monotonic() - started == pytest.approx(3600)
    assert time.perf_counter() - started_real < 1


async def test_timers_fire_in_order_at_their_moments() -> None:
    fired: list[tuple[str, float]] = []

    async def at(delay: float, name: str) -> None:
        await asyncio.sleep(delay)
        fired.append((name, clock.monotonic()))

    await asyncio.gather(at(10, "late"), at(5, "early"), at(7.5, "middle"))

    assert [name for name, _ in fired] == ["early", "middle", "late"]
    assert [moment for _, moment in fired] == pytest.approx([5, 7.5, 10])


async def test_timeout_fires_at_its_virtual_deadline() -> None:
    never: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    started = clock.monotonic()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(30):
            await never

    assert clock.monotonic() - started == pytest.approx(30)


async def test_real_io_is_still_awaited() -> None:
    # Колбэк из другого потока приходит по настоящему I/O цикла: время не должно «проскочить».
    loop = asyncio.get_running_loop()
    done: asyncio.Future[str] = loop.create_future()

    def worker() -> None:
        time.sleep(0.05)
        loop.call_soon_threadsafe(done.set_result, "ok")

    threading.Thread(target=worker).start()

    assert await done == "ok"


def test_loop_can_start_at_any_moment() -> None:
    loop = VirtualTimeLoop(start=1_000.0)
    try:
        assert loop.time() == 1_000.0
        loop.run_until_complete(asyncio.sleep(5))
        assert loop.time() == pytest.approx(1_005.0)
    finally:
        loop.close()


def test_monotonic_requires_running_loop() -> None:
    with pytest.raises(RuntimeError):
        clock.monotonic()


async def test_thread_work_is_awaited_for_real() -> None:
    # Пока поток работает, время не прыгает: таймаут вокруг to_thread не срабатывает раньше срока.
    def slow() -> str:
        time.sleep(0.05)
        return "ok"

    async with asyncio.timeout(30):
        assert await asyncio.to_thread(slow) == "ok"
