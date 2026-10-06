"""Аренда контекста целиком и эксклюзивность identity."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager

import pytest

from browser_pool import BrowserPool, ErrorKind, Identity, PoolConfig, clock
from browser_pool.config import Limits, Recycling, Timeouts, Topology
from browser_pool.errors import IdentityBusyError
from browser_pool.events import ContextRetired, PoolEvent
from browser_pool.lease import ContextLease, PageLease
from browser_pool.locks import LocalIdentityLock
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

A = Identity(key="mail:a")
B = Identity(key="mail:b")

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


def make_pool(
    driver: FakeDriver,
    *,
    pages: int = 4,
    lock: LocalIdentityLock | None = None,
    open_timeout: float = 30.0,
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=pages),
            limits=Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            timeouts=Timeouts(open=open_timeout),
        ),
        identity_lock=lock,
    )


async def hold_context(pool: Pool, identity: Identity, release: asyncio.Event) -> float:
    async with pool.context(identity):
        await release.wait()
        return clock.monotonic()


async def hold_page(pool: Pool, identity: Identity, release: asyncio.Event) -> float:
    async with pool.page(identity):
        await release.wait()
        return clock.monotonic()


async def acquired_at(lease_cm: AbstractAsyncContextManager[object]) -> float:
    async with lease_cm:
        return clock.monotonic()


# --- выдача ----------------------------------------------------------------------------


async def test_context_lease_gives_the_whole_context(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool, pool.context(A) as lease:
        assert isinstance(lease, ContextLease)
        assert not isinstance(lease, PageLease)
        assert lease.context.alive
        assert lease.browser.alive
        page = await fake_driver.new_page(lease.context)  # вкладками управляет сам арендатор
        await fake_driver.close_page(page)
        assert pool.snapshot().leases_active == 1


async def test_context_lease_takes_one_page_slot(fake_driver: FakeDriver) -> None:
    release = asyncio.Event()

    async with make_pool(fake_driver, pages=1) as pool:
        holder = asyncio.create_task(hold_context(pool, A, release))
        await asyncio.sleep(0)
        other = asyncio.create_task(acquired_at(pool.page(B)))
        await asyncio.sleep(5)
        assert not other.done()  # единственный слот браузера занят арендой контекста

        release.set()
        await holder
        await other


# --- эксклюзивность --------------------------------------------------------------------


async def test_second_context_lease_waits_for_the_first(fake_driver: FakeDriver) -> None:
    release = asyncio.Event()

    async with make_pool(fake_driver) as pool:
        first = asyncio.create_task(hold_context(pool, A, release))
        await asyncio.sleep(0)
        second = asyncio.create_task(acquired_at(pool.context(A)))
        await asyncio.sleep(10)
        assert not second.done()

        release.set()
        released_at = await first
        assert await second >= released_at


async def test_context_lease_waits_for_pages_and_blocks_new_ones(fake_driver: FakeDriver) -> None:
    release_page, release_context = asyncio.Event(), asyncio.Event()

    async with make_pool(fake_driver) as pool:
        page = asyncio.create_task(hold_page(pool, A, release_page))
        await asyncio.sleep(0)
        whole = asyncio.create_task(hold_context(pool, A, release_context))
        await asyncio.sleep(1)
        latecomer = asyncio.create_task(acquired_at(pool.page(A)))
        neighbour = await acquired_at(pool.page(B))  # чужая identity не ждёт
        await asyncio.sleep(1)
        assert not whole.done()
        assert not latecomer.done()  # новые вкладки A не обгоняют ждущую аренду контекста

        release_page.set()
        page_released = await page
        await asyncio.sleep(1)
        assert not latecomer.done()  # контекст отдан целиком
        release_context.set()
        context_released = await whole
        assert await latecomer >= context_released >= page_released
        assert neighbour < page_released


async def test_context_takes_any_of_and_leases_one_candidate(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool, pool.context(any_of=[A, B]) as lease:
        assert lease.identity in {A, B}


async def test_context_needs_exactly_one_of_identity_and_any_of(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        with pytest.raises(ValueError, match="ровно одно"):
            async with pool.context():
                pass


async def test_abandoned_context_request_does_not_block_pages(fake_driver: FakeDriver) -> None:
    release = asyncio.Event()

    async with make_pool(fake_driver) as pool:
        page = asyncio.create_task(hold_page(pool, A, release))
        await asyncio.sleep(0)
        with pytest.raises(TimeoutError):
            async with pool.context(A, acquire_timeout=1.0):
                pass

        async with pool.page(A) as lease:  # удержание снято вместе с ушедшей заявкой
            assert lease.identity == A
        release.set()
        await page


async def test_reported_fault_on_a_context_lease_is_handled(fake_driver: FakeDriver) -> None:
    events: list[PoolEvent] = []

    async with make_pool(fake_driver) as pool:
        pool.on(ContextRetired, events.append)
        async with pool.context(A) as lease:
            lease.report(ErrorKind.session)

    assert [event.key for event in events if isinstance(event, ContextRetired)] == ["mail:a"]


# --- замок identity между пулами --------------------------------------------------------


async def test_shared_lock_keeps_identity_in_one_pool(fake_driver: FakeDriver) -> None:
    lock = LocalIdentityLock()
    first = make_pool(fake_driver, lock=lock)
    second = make_pool(fake_driver, lock=lock, open_timeout=5.0)

    async with first, second:
        async with first.page(A):
            pass
        assert lock.held("mail:a")  # контекст жив — identity занята

        with pytest.raises(IdentityBusyError) as caught:
            async with second.page(A, wait_cooldown=False):
                pass
        assert caught.value.identity == "mail:a"
        async with second.page(B):  # другие identity второй пул открывает свободно
            pass

    assert not lock.held("mail:a")  # пул закрыл контекст — замок отпущен


async def test_local_lock_rejects_releasing_what_is_not_held() -> None:
    lock = LocalIdentityLock()

    await lock.acquire("mail:a")
    assert lock.held("mail:a")
    await lock.release("mail:a")

    assert not lock.held("mail:a")
    with pytest.raises(RuntimeError, match="не занята"):
        await lock.release("mail:a")
