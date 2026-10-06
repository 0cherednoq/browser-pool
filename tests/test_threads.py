"""Поток на браузер: синхронный SDK вызывается только из потока своего браузера.

Фейк с `thread_affinity` сам падает на вызове по браузеру не из его потока, поэтому любой
сценарий, который проходит, — уже проверка. Здесь — то, чего сценарии не видят: какие потоки,
`lease.call`, `ctx.call` во flow, отключение из чужого потока, окна.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections.abc import AsyncIterator
from typing import Any, cast, override

import pytest
import pytest_asyncio

from browser_pool import BaseFlow, BrowserPool, Identity, PoolConfig
from browser_pool._core.threads import bind_threads
from browser_pool.config import Lifecycle, Limits, Recycling, Timeouts, Topology, Windows
from browser_pool.driver import ContextSpec, LaunchSpec, PageLabeler, WindowControl
from browser_pool.flow import OpenRequest
from browser_pool.snapshot import BrowserState
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakeDriverError, FakePage
from browser_pool.testing.fake_driver import FAKE_CAPABILITIES

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

SYNC = dataclasses.replace(FAKE_CAPABILITIES, thread_affinity=True)
A = Identity(key="mail:a")
B = Identity(key="mail:b")


@pytest_asyncio.fixture
async def driver() -> AsyncIterator[FakeDriver]:
    """Синхронный фейк; после теста — ничего не утекло и потоки браузеров остановлены."""
    fake = FakeDriver(capabilities=SYNC)
    yield fake
    await asyncio.sleep(0)
    assert tuple(fake.live) == (0, 0, 0), f"утечка: {fake.live}"
    for thread in threading.enumerate():
        if thread.name.startswith("browser-pool-"):
            thread.join(timeout=5)
    assert not [t.name for t in threading.enumerate() if t.name.startswith("browser-pool-")]


def make_pool(driver: FakeDriver, *, browsers: int = 2, **config: object) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=browsers, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            lifecycle=Lifecycle(healthcheck_interval=10.0),
            recycling=Recycling(recycle_jitter=0.0),
            timeouts=Timeouts(close=2.0, restart=5.0, startup=5.0),
            **config,  # pyright: ignore[reportArgumentType] — секции конфига
        ),
    )


async def test_every_call_of_a_browser_comes_from_its_own_thread(driver: FakeDriver) -> None:
    release = asyncio.Event()

    async def hold(identity: Identity) -> FakeBrowser:
        async with pool.page(identity) as lease:
            await release.wait()
            return lease.browser

    async with make_pool(driver) as pool:
        tasks = [asyncio.create_task(hold(identity)) for identity in (A, B)]
        await asyncio.sleep(0.1)
        release.set()
        first, second = await asyncio.gather(*tasks)
        async with pool.page(A) as lease:
            await lease.retire_context("проверка")

    main = threading.get_ident()
    assert first is not second
    assert len({first.thread, second.thread, main}) == 3
    for call in driver.calls:
        target = call.target
        owner = (
            target
            if isinstance(target, FakeBrowser)
            else target.browser
            if isinstance(target, FakeContext)
            else target.context.browser
            if isinstance(target, FakePage)
            else None
        )
        if owner is not None:
            assert call.thread == owner.thread, call
        else:  # prepare, shutdown, launch — не в цикле пула
            assert call.thread != main, call


async def test_lease_call_runs_sync_code_in_the_browser_thread(driver: FakeDriver) -> None:
    def where(page: FakePage, *, suffix: str) -> tuple[int, str]:
        page.url = f"https://example.com/{suffix}"
        return threading.get_ident(), page.url

    def broken() -> None:
        msg = "sdk"
        raise RuntimeError(msg)

    async with make_pool(driver) as pool, pool.page(A) as lease:
        thread, url = await lease.call(where, lease.page, suffix="inbox")
        assert thread == lease.browser.thread != threading.get_ident()
        assert url == lease.page.url == "https://example.com/inbox"
        with pytest.raises(RuntimeError, match="sdk"):
            await lease.call(broken)


async def test_lease_call_without_thread_affinity_runs_inline(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool, pool.page(A) as lease:
        assert await lease.call(threading.get_ident) == threading.get_ident()


async def test_flow_logs_in_with_sync_code_in_the_browser_thread(driver: FakeDriver) -> None:
    seen: list[int] = []

    class SyncLogin(BaseFlow[FakeContext, FakePage, str]):
        @override
        async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> str:
            page = await ctx.new_page()
            seen.append(await ctx.call(threading.get_ident))
            await ctx.call(setattr, page, "url", "https://example.com/login")
            return "session"

    pool = BrowserPool(driver, config=PoolConfig(limits=Limits(spawn_delay=0.0)), flow=SyncLogin())
    async with pool, pool.page(A) as lease:
        assert lease.session == "session"
        assert seen == [lease.browser.thread]


async def test_disconnect_from_the_browser_thread_reaches_the_pool(driver: FakeDriver) -> None:
    async with make_pool(driver, browsers=1) as pool:
        async with pool.page(A) as lease:
            crashed = lease.browser
            # Падение приходит от SDK в его потоке — пул узнаёт о нём в своём цикле.
            await lease.call(driver.crash, crashed)
        await asyncio.sleep(1.0)

        assert browser_states(pool) == {"browser-0": BrowserState.healthy}
        async with pool.page(A) as lease:
            assert lease.browser is not crashed
            assert lease.browser.thread != crashed.thread


async def test_windows_move_in_the_browser_thread(driver: FakeDriver) -> None:
    windowed = FakeDriver(
        capabilities=dataclasses.replace(SYNC, window_control="runtime", new_window=True)
    )
    pool = make_pool(windowed, browsers=1, windows=Windows(mode="per_context", gap=8))
    async with pool:
        async with pool.page(A), pool.page(B):
            pass
        await pool.windows.retile()
    assert any(call.operation == "set_bounds" for call in windowed.calls)
    assert tuple(windowed.live) == (0, 0, 0)


async def test_bound_driver_keeps_the_optional_parts(driver: FakeDriver) -> None:
    bound = bind_threads(driver)
    assert isinstance(bound, WindowControl)
    assert isinstance(bound, PageLabeler)

    browser = await bound.launch(LaunchSpec())
    context = await bound.new_context(browser, ContextSpec())
    page = await bound.new_page(context)
    await cast("PageLabeler[FakePage]", bound).label_page(page, "[a] ")
    assert page.label == "[a] "
    assert bound.pid(browser) is None

    with pytest.raises(FakeDriverError, match="не из его потока"):
        await driver.close_page(page)  # мимо обёртки — из цикла пула
    await bound.close_browser(browser)


def browser_states(pool: BrowserPool[Any, Any, Any]) -> dict[str, BrowserState]:
    """Состояние каждого браузера — из снимка пула."""
    return {browser.id: browser.state for browser in pool.snapshot().browsers}
