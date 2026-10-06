"""Отмена на выходе из аренды и при открытии контекста: слот, вкладка и контекст не теряются.

Арендатора отменяют когда угодно — `pool.map` отменяет соседей упавшей задачи, снаружи стоит
`asyncio.timeout`, приложение останавливается. Хвост аренды (улики, хуки, сброс вкладки) при этом
доделывает пул, а не отменённая задача.
"""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest

from browser_pool import BaseFlow, BrowserPool, Identity, OpenRequest, PageLease, PoolConfig
from browser_pool._core.tabs import TabPool
from browser_pool.config import Limits, Timeouts, Topology
from browser_pool.driver import ContextSpec, Evidence, LaunchSpec
from browser_pool.proxies import Proxy, ProxyLease, ProxyOutcome, ProxyRequest
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]
type Lease = PageLease[FakeBrowser, FakeContext, FakePage, Any]

A = Identity(key="mail:a")
SLOW = 5.0
"""Сколько длится медленная операция хвоста; отмена приходит на её середине."""


class SlowSink:
    """Приёмник улик, который пишет долго — как скриншот в сетевое хранилище."""

    async def save(self, evidence: Evidence, *, key: str, lease_id: int, error: str) -> str:
        _ = evidence, key, error
        await asyncio.sleep(SLOW)
        return f"evidence-{lease_id}"


class Flow(BaseFlow[FakeContext, FakePage, str]):
    """Считает входы; сброс вкладки может длиться."""

    def __init__(self, *, reset_takes: float = 0.0) -> None:
        self.opens = 0
        self.reset_takes = reset_takes

    @override
    async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> str:
        self.opens += 1
        return ctx.identity.key

    @override
    async def reset_page(self, session: str, page: FakePage) -> bool:
        await asyncio.sleep(self.reset_takes)
        return True


def make_pool(driver: FakeDriver, **parts: Any) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            timeouts=Timeouts(close=SLOW * 2, reset_page=SLOW * 2),
        ),
        **parts,
    )


async def fail(pool: Pool, identity: Identity = A) -> None:
    async with pool.page(identity):
        msg = "задача упала"
        raise RuntimeError(msg)


async def cancel_midway(task: asyncio.Task[None]) -> None:
    """Отменить задачу, когда её хвост аренды уже идёт, и дождаться её и хвоста."""
    await asyncio.sleep(SLOW / 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(SLOW)


# --- выход из аренды --------------------------------------------------------------------


async def test_cancel_while_capturing_evidence_returns_the_lease(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, evidence_sink=SlowSink()) as pool:
        task = asyncio.create_task(fail(pool))

        await cancel_midway(task)

        assert task.cancelled()
        assert pool.snapshot().leases_active == 0
        assert fake_driver.live.pages == 0  # вкладка упавшей аренды закрыта, а не забыта


async def test_map_cancelling_siblings_leaves_no_lease(fake_driver: FakeDriver) -> None:
    identities = [Identity(key=f"mail:{index}") for index in range(3)]

    async def task(lease: Lease) -> None:
        # Первая падает сразу и ещё снимает улики, когда падает вторая и `map` отменяет всех.
        await asyncio.sleep(0.0 if lease.identity is identities[0] else SLOW / 2)
        msg = "задача упала"
        raise RuntimeError(msg)

    async with make_pool(fake_driver, evidence_sink=SlowSink()) as pool:
        with pytest.raises(RuntimeError):
            await pool.map(task, identities, concurrency=3)
        await asyncio.sleep(SLOW * 2)

        assert pool.snapshot().leases_active == 0


async def test_cancel_inside_before_release_hook_returns_the_lease(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)

    @pool.hooks.before_release
    async def slow_hook(lease: Lease) -> None:
        _ = lease
        await asyncio.sleep(SLOW)

    async def work() -> None:
        async with pool.page(A):
            pass

    async with pool:
        task = asyncio.create_task(work())

        await cancel_midway(task)

        assert task.cancelled()
        assert pool.snapshot().leases_active == 0
        assert fake_driver.live.pages == 1  # хук доработал: вкладка вернулась тёплой


async def test_cancel_during_page_reset_keeps_the_page_accounted(fake_driver: FakeDriver) -> None:
    async def work(pool: Pool, seen: list[FakePage]) -> None:
        async with pool.page(A) as lease:
            seen.append(lease.page)

    async with make_pool(fake_driver, flow=Flow(reset_takes=SLOW)) as pool:
        seen: list[FakePage] = []
        task = asyncio.create_task(work(pool, seen))

        await cancel_midway(task)
        async with pool.page(A) as lease:
            again = lease.page

        assert pool.snapshot().leases_active == 0
        assert again is seen[0]
        assert fake_driver.live.pages == 1


async def test_tail_of_a_cancelled_lease_does_not_outlive_the_pool(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver, evidence_sink=SlowSink())
    await pool.start()
    task = asyncio.create_task(fail(pool))
    await asyncio.sleep(SLOW / 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    await pool.stop()  # остановка дожидается хвоста: фикстура проверит, что ничего не утекло

    assert pool.snapshot().leases_active == 0


async def test_give_back_cancelled_in_reset_closes_the_page(fake_driver: FakeDriver) -> None:
    async def reset(page: FakePage) -> bool:
        _ = page
        await asyncio.sleep(SLOW)
        return True

    browser = await fake_driver.launch(LaunchSpec())
    context = await fake_driver.new_context(browser, ContextSpec())
    tabs = TabPool(
        fake_driver,
        context,
        prepare=None,
        reset=reset,
        warm_limit=1,
        timeouts=Timeouts(reset_page=SLOW * 2),
    )
    page = await tabs.take()
    task = asyncio.create_task(tabs.give_back(page))
    await asyncio.sleep(SLOW / 2)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert tabs.open == 0
    assert not page.alive
    await fake_driver.close_browser(browser)


# --- открытие контекста -----------------------------------------------------------------


class SlowReports:
    """Источник прокси, который долго принимает отчёт об успехе."""

    def __init__(self) -> None:
        self.proxy = Proxy(host="10.0.0.1", port=8080, id="p1")

    async def acquire(self, request: ProxyRequest) -> ProxyLease | None:
        return ProxyLease.of(self.proxy, request.identity_key)

    async def report(self, lease: ProxyLease, outcome: ProxyOutcome) -> None:
        _ = lease, outcome
        await asyncio.sleep(SLOW)

    async def release(self, lease: ProxyLease) -> None:
        _ = lease


async def test_cancel_after_the_session_opened_keeps_the_context(fake_driver: FakeDriver) -> None:
    flow = Flow()

    async def work(pool: Pool) -> None:
        async with pool.page(A):
            pass

    async with make_pool(fake_driver, flow=flow, proxy_source=SlowReports()) as pool:
        task = asyncio.create_task(work(pool))
        await asyncio.sleep(SLOW / 2)  # вход прошёл, идёт отчёт источнику прокси
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        async with pool.page(A):
            pass

        assert flow.opens == 1  # открытый контекст не потерян: второго входа нет
        assert fake_driver.live.contexts == 1


# --- учёт по ключу identity ---------------------------------------------------------------


async def test_one_off_identities_leave_nothing_behind(fake_driver: FakeDriver) -> None:
    config = PoolConfig.scraping().replace(
        topology=Topology(browsers=1, pages_per_browser=4, warm_pages_per_identity=0),
        limits=Limits(spawn_delay=0.0),
    )
    pool: Pool = BrowserPool(fake_driver, config=config)

    async with pool:
        for index in range(300):
            async with pool.page(Identity(key=f"anon:{index}")):
                pass
        await asyncio.sleep(120.0)  # контексты закрыты по простою

        assert pool.snapshot().contexts == ()
        scheduler = pool._scheduler  # pyright: ignore[reportPrivateUsage]
        resources = pool._resources  # pyright: ignore[reportPrivateUsage]
        assert scheduler.remembered_identities == frozenset()
        assert resources.remembered_identities == frozenset()
