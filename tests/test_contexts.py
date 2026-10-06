"""Физические ресурсы: браузеры лениво, контексты по поколениям, место для вкладок, закрытие."""

from __future__ import annotations

import asyncio

import pytest

from browser_pool import clock
from browser_pool._core.contexts import PhysicalResources
from browser_pool._core.scheduler import Grant, Scheduler
from browser_pool.config import Lifecycle, Timeouts, Topology
from browser_pool.driver import ContextSpec, LaunchSpec
from browser_pool.errors import StaleLeaseError
from browser_pool.identity import Identity
from browser_pool.testing import FakeDriver, FakePage

A = Identity(key="mail:a")
B = Identity(key="mail:b")
TIMEOUTS = Timeouts(startup=10.0, context_create=5.0, page_create=5.0, close=2.0, kill=1.0)


class Harness:
    """Планировщик и физический слой в связке — как их соединит фасад."""

    def __init__(self, driver: FakeDriver, *, topology: Topology | None = None) -> None:
        self.topology = topology or Topology(
            browsers=1, pages_per_browser=4, warm_pages_per_identity=2
        )
        self.scheduler = Scheduler(self.topology)
        self.driver = driver
        self.resources = PhysicalResources(
            driver,
            topology=self.topology,
            timeouts=TIMEOUTS,
            launch_spec=lambda browser_id: LaunchSpec(extra={"browser_id": browser_id}),
            context_spec=lambda identity: ContextSpec(extra={"key": identity.key}),
        )

    def grant(self, identity: Identity) -> Grant:
        waiter = self.scheduler.request([identity], now=clock.monotonic())
        assert waiter.grant is not None
        return waiter.grant

    async def page(self, identity: Identity) -> tuple[Grant, FakePage]:
        grant = self.grant(identity)
        return grant, await self.resources.acquire_page(grant)

    async def release(self, grant: Grant, page: FakePage, *, discard: bool = False) -> None:
        await self.resources.release_page(grant, page, discard=discard)
        self.scheduler.release(grant, now=clock.monotonic())

    async def close_retired(self) -> None:
        for context in self.scheduler.take_closures():
            await self.resources.close_context(context.key, generation=context.generation)
            self.scheduler.forget(context.key, generation=context.generation)

    async def shutdown(self) -> None:
        await self.resources.aclose()


def calls(driver: FakeDriver, operation: str) -> int:
    return sum(call.operation == operation for call in driver.calls)


# --- ленивое создание ------------------------------------------------------------------


async def test_browser_and_context_are_created_once_and_reused(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)

    first_grant, first = await pool.page(A)
    await pool.release(first_grant, first)
    second_grant, second = await pool.page(A)
    _, neighbour = await pool.page(B)

    assert second is first  # тёплая вкладка
    assert calls(fake_driver, "launch") == 1
    assert calls(fake_driver, "new_context") == 2
    assert fake_driver.browsers[0].spec is not None
    assert fake_driver.browsers[0].spec.extra == {"browser_id": "browser-0"}
    assert neighbour.context.spec.extra == {"key": "mail:b"}
    await pool.release(second_grant, second)
    await pool.shutdown()


async def test_concurrent_first_leases_of_one_identity_share_one_context(
    fake_driver: FakeDriver,
) -> None:
    pool = Harness(fake_driver)
    fake_driver.faults.delay("launch", 1.0)
    fake_driver.faults.delay("new_context", 1.0)

    results = await asyncio.gather(pool.page(A), pool.page(A), pool.page(A))

    assert calls(fake_driver, "launch") == 1
    assert calls(fake_driver, "new_context") == 1
    assert len({page.context.id for _, page in results}) == 1
    await pool.shutdown()


async def test_failed_launch_is_retried_by_the_next_lease(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    fake_driver.faults.fail("launch", RuntimeError("нет бинаря"))

    grant = pool.grant(A)
    with pytest.raises(RuntimeError, match="бинаря"):
        await pool.resources.acquire_page(grant)
    # Как фасад: контекст, который не открылся, выводится из работы; следующая аренда — нового поколения.
    pool.scheduler.retire(A.key, generation=grant.generation)
    pool.scheduler.release(grant, now=clock.monotonic())
    await pool.close_retired()

    _, page = await pool.page(A)
    assert page.alive
    await pool.shutdown()


async def test_hanging_launch_times_out(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    fake_driver.faults.hang("launch")
    started = clock.monotonic()

    with pytest.raises(TimeoutError):
        await pool.resources.acquire_page(pool.grant(A))

    assert clock.monotonic() - started == pytest.approx(TIMEOUTS.startup)
    await pool.shutdown()


# --- поколения -------------------------------------------------------------------------


async def test_new_generation_replaces_the_physical_context(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant, page = await pool.page(Identity(key="mail:a", variant="proxy"))
    await pool.release(grant, page)

    switched, fresh = await pool.page(Identity(key="mail:a", variant="direct"))

    assert switched.generation == grant.generation + 1
    assert fresh.context is not page.context
    assert page.context.closed
    assert fake_driver.live.contexts == 1
    await pool.release(switched, fresh)
    await pool.shutdown()


async def test_lease_of_an_older_generation_is_stale(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant, page = await pool.page(Identity(key="mail:a", variant="proxy"))
    await pool.release(grant, page)
    switched, fresh = await pool.page(Identity(key="mail:a", variant="direct"))

    with pytest.raises(StaleLeaseError):
        await pool.resources.acquire_page(grant)

    await pool.release(switched, fresh)
    await pool.shutdown()


# --- место для вкладок -----------------------------------------------------------------


async def test_full_browser_closes_a_neighbours_coldest_warm_page(fake_driver: FakeDriver) -> None:
    pool = Harness(
        fake_driver, topology=Topology(browsers=1, pages_per_browser=2, warm_pages_per_identity=2)
    )
    leases = [await pool.page(A), await pool.page(A)]
    for grant, page in leases:
        await pool.release(grant, page)
    assert fake_driver.live.pages == 2  # обе вкладки A тёплые, браузер полон

    grant_b, page_b = await pool.page(B)

    assert fake_driver.live.pages == 2
    assert not leases[0][1].alive  # самая холодная вкладка соседа уступила место
    assert leases[1][1].alive
    await pool.release(grant_b, page_b)
    await pool.shutdown()


async def test_discarded_page_is_closed(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant, page = await pool.page(A)

    await pool.release(grant, page, discard=True)

    assert not page.alive
    await pool.shutdown()


# --- закрытие --------------------------------------------------------------------------


async def test_retiring_a_context_leaves_neighbours_alone(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant_a, page_a = await pool.page(A)
    grant_b, page_b = await pool.page(B)

    pool.scheduler.retire("mail:a", generation=grant_a.generation)
    await pool.release(grant_a, page_a)
    await pool.close_retired()

    assert page_a.context.closed
    assert page_b.alive
    assert fake_driver.live == (1, 1, 1)
    await pool.release(grant_b, page_b)
    await pool.shutdown()


async def test_hanging_context_close_neither_blocks_nor_raises(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant, page = await pool.page(A)
    await pool.release(grant, page, discard=True)
    fake_driver.faults.hang("close_context")
    started = clock.monotonic()

    await pool.resources.close_context("mail:a", generation=grant.generation)

    assert clock.monotonic() - started == pytest.approx(TIMEOUTS.close)
    assert pool.resources.close_failures == 1
    assert pool.resources.open_contexts() == []
    await pool.shutdown()


async def test_contexts_close_concurrently_so_hangs_cost_one_timeout(
    fake_driver: FakeDriver,
) -> None:
    pool = Harness(fake_driver)
    grant_a, page_a = await pool.page(A)
    grant_b, page_b = await pool.page(B)
    await pool.release(grant_a, page_a, discard=True)
    await pool.release(grant_b, page_b, discard=True)
    fake_driver.faults.hang("close_context", times=2)
    started = clock.monotonic()

    await pool.shutdown()

    assert clock.monotonic() - started == pytest.approx(TIMEOUTS.close)
    assert pool.resources.close_failures == 2
    assert pool.resources.open_contexts() == []
    assert fake_driver.live == (0, 0, 0)


async def test_browser_that_will_not_close_is_killed(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant, page = await pool.page(A)
    await pool.release(grant, page)
    fake_driver.faults.hang("close_browser")

    await pool.shutdown()

    assert calls(fake_driver, "kill_browser") == 1
    assert pool.resources.close_failures == 1
    assert fake_driver.live == (0, 0, 0)


async def test_idle_pages_expire_across_contexts(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant_a, page_a = await pool.page(A)
    await pool.release(grant_a, page_a)
    await asyncio.sleep(400)
    grant_b, page_b = await pool.page(B)
    await pool.release(grant_b, page_b)

    closed = await pool.resources.expire_idle_pages(idle_ttl=300.0)

    assert closed == 1
    assert not page_a.alive
    assert page_b.alive
    await pool.shutdown()


# --- планировщик: устаревание контекстов -----------------------------------------------


async def test_idle_contexts_are_retired_by_age(fake_driver: FakeDriver) -> None:
    pool = Harness(fake_driver)
    grant_a, page_a = await pool.page(A)
    await pool.release(grant_a, page_a)
    await asyncio.sleep(700)
    grant_b, page_b = await pool.page(B)

    retired = pool.scheduler.retire_idle(
        now=clock.monotonic(), idle_ttl=Lifecycle().context_idle_ttl or 0
    )

    assert retired == 1
    assert [context.key for context in pool.scheduler.take_closures()] == ["mail:a"]
    await pool.resources.close_context("mail:a", generation=grant_a.generation)
    pool.scheduler.forget("mail:a", generation=grant_a.generation)
    await pool.release(grant_b, page_b)
    await pool.shutdown()
