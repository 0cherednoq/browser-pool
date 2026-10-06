"""Супервизор: падения, зависания, восстановление с паузами, плановый перезапуск, простой."""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool._core.supervisor import check_transition
from browser_pool.config import (
    Backoff,
    Lifecycle,
    Limits,
    Recovery,
    Recycling,
    Timeouts,
    Topology,
)
from browser_pool.errors import PoolInvariantError, PoolUnavailableError
from browser_pool.events import BrowserRestarted, PoolHealth, StateSaveFailed
from browser_pool.snapshot import BrowserState
from browser_pool.state import IdentityRecord, MemoryStateStore
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

A = Identity(key="mail:a")
B = Identity(key="mail:b")

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


def make_pool(
    driver: FakeDriver,
    *,
    topology: Topology | None = None,
    lifecycle: Lifecycle | None = None,
    recycling: Recycling | None = None,
    recovery: Recovery | None = None,
    limits: Limits | None = None,
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=topology or Topology(browsers=1, pages_per_browser=4),
            limits=limits or Limits(spawn_delay=0.0),
            lifecycle=lifecycle or Lifecycle(healthcheck_interval=10.0),
            recycling=recycling
            or Recycling(browser_max_leases=None, browser_max_age=None, recycle_jitter=0.0),
            recovery=recovery
            or Recovery(restart_backoff=Backoff(initial=1.0, maximum=4.0, factor=2.0, jitter=0.0)),
            timeouts=Timeouts(ping=2.0, close=2.0, restart=5.0, startup=5.0),
        ),
    )


class FlakyStore(MemoryStateStore):
    """Хранилище, запись в которое можно сломать."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    @override
    async def save(self, record: IdentityRecord) -> IdentityRecord:
        if self.broken:
            msg = "диск недоступен"
            raise OSError(msg)
        return await super().save(record)


def launches(driver: FakeDriver) -> int:
    return sum(call.operation == "launch" for call in driver.calls)


async def settle(seconds: float = 1.0) -> None:
    await asyncio.sleep(seconds)


# --- машина состояний ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (BrowserState.quarantined, BrowserState.healthy),  # мимо восстановления
        (BrowserState.stopped, BrowserState.draining),
        (BrowserState.healthy, BrowserState.restarting),
    ],
)
def test_invalid_transitions_are_invariant_violations(
    current: BrowserState, target: BrowserState
) -> None:
    with pytest.raises(PoolInvariantError):
        check_transition(current, target)


# --- падение ---------------------------------------------------------------------------


async def test_crashed_browser_is_replaced(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            crashed = lease.browser
        fake_driver.crash(crashed)
        await settle()

        async with pool.page(A) as lease:
            assert lease.browser is not crashed
            assert lease.page.alive
            assert lease.generation == 2  # контекст пересоздан в новом процессе
        assert launches(fake_driver) == 2
        assert browser_states(pool) == {"browser-0": BrowserState.healthy}


async def test_crash_under_a_lease_waits_for_it_and_spares_the_other_browser(
    fake_driver: FakeDriver,
) -> None:
    pool = make_pool(fake_driver, topology=Topology(browsers=2, pages_per_browser=4))
    async with pool:
        release = asyncio.Event()

        async def hold(identity: Identity) -> FakeBrowser:
            async with pool.page(identity) as lease:
                await release.wait()
                return lease.browser

        first = asyncio.create_task(hold(A))
        second = asyncio.create_task(hold(B))
        await settle()
        victim = fake_driver.browsers[0]
        fake_driver.crash(victim)
        await settle()

        states = browser_states(pool)
        assert sorted(states.values()) == [BrowserState.healthy, BrowserState.quarantined]
        assert launches(fake_driver) == 2  # перезапуск ждёт, пока вернут аренду

        release.set()
        survivors = await asyncio.gather(first, second)
        await settle()
        assert launches(fake_driver) == 3
        assert all(state is BrowserState.healthy for state in browser_states(pool).values())
        assert any(browser is not victim and browser.alive for browser in survivors)


# --- зависание -------------------------------------------------------------------------


async def test_hung_browser_is_found_by_the_health_check(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A):
            pass
        fake_driver.faults.hang("ping")

        await settle(10.0 + 2.0 + 1.0)  # интервал проверки + таймаут ping

        assert launches(fake_driver) == 2
        assert browser_states(pool) == {"browser-0": BrowserState.healthy}


# --- восстановление --------------------------------------------------------------------


async def test_restart_retries_with_growing_pauses(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            browser = lease.browser
        fake_driver.faults.fail("launch", RuntimeError("не встаёт"), times=2)
        fake_driver.crash(browser)

        await settle(0.5)
        assert browser_states(pool) == {"browser-0": BrowserState.quarantined}
        await settle(1.0 + 2.0)  # паузы 1 и 2 секунды

        assert browser_states(pool) == {"browser-0": BrowserState.healthy}
        assert launches(fake_driver) == 4


async def test_pool_gives_up_and_says_so(fake_driver: FakeDriver) -> None:
    recycling = Recycling(browser_max_leases=None, browser_max_age=None)
    recovery = Recovery(
        restart_max_attempts=2,
        restart_backoff=Backoff(initial=1.0, maximum=60.0, factor=2.0, jitter=0.0),
    )
    async with make_pool(fake_driver, recycling=recycling, recovery=recovery) as pool:
        async with pool.page(A) as lease:
            browser = lease.browser
        fake_driver.faults.fail("launch", RuntimeError("не встаёт"), times=10)

        async def wait_for_page() -> None:
            async with pool.page(B):
                pass

        fake_driver.crash(browser)
        waiter = asyncio.create_task(wait_for_page())
        await settle(10.0)

        with pytest.raises(PoolUnavailableError):
            await waiter
        with pytest.raises(PoolUnavailableError):
            async with pool.page(A):
                pass


async def test_pool_comes_back_when_the_launch_works_again(fake_driver: FakeDriver) -> None:
    recovery = Recovery(
        restart_max_attempts=2,
        restart_backoff=Backoff(initial=1.0, maximum=30.0, factor=2.0, jitter=0.0),
    )
    restarted: list[BrowserRestarted] = []
    async with make_pool(fake_driver, recovery=recovery) as pool:
        pool.on(BrowserRestarted, restarted.append)
        async with pool.page(A) as lease:
            browser = lease.browser
        fake_driver.faults.fail("launch", RuntimeError("не встаёт"), times=2)  # временный сбой
        fake_driver.crash(browser)

        await settle(10.0)  # попытки кончились: пул недоступен
        assert browser_states(pool) == {"browser-0": BrowserState.quarantined}
        with pytest.raises(PoolUnavailableError):
            async with pool.page(A):
                pass

        await settle(40.0)  # следующая серия — через restart_backoff.maximum, из проверки здоровья

        assert browser_states(pool) == {"browser-0": BrowserState.healthy}
        assert len(restarted) == 1
        async with pool.page(A) as lease:
            assert lease.browser.alive


async def test_recovery_switched_off_stays_off(fake_driver: FakeDriver) -> None:
    recovery = Recovery(restart_max_attempts=0, restart_backoff=Backoff.fixed(1.0))
    async with make_pool(fake_driver, recovery=recovery) as pool:
        async with pool.page(A) as lease:
            browser = lease.browser
        fake_driver.crash(browser)

        await settle(60.0)

        assert browser_states(pool) == {"browser-0": BrowserState.quarantined}
        assert launches(fake_driver) == 1


async def test_broken_state_store_does_not_stop_the_health_check(fake_driver: FakeDriver) -> None:
    store = FlakyStore()
    lifecycle = Lifecycle(healthcheck_interval=10.0, state_save_interval=10.0, page_idle_ttl=20.0)
    pool: Pool = BrowserPool(
        fake_driver,
        config=PoolConfig(
            topology=Topology(browsers=1), limits=Limits(spawn_delay=0.0), lifecycle=lifecycle
        ),
        state_store=store,
    )
    checks: list[PoolHealth] = []
    unsaved: list[StateSaveFailed] = []
    pool.on(PoolHealth, checks.append)
    pool.on(StateSaveFailed, unsaved.append)

    async with pool:
        async with pool.page(A):
            pass
        async with pool.context(B):  # контекст B занят: его состояние сохраняется по расписанию
            store.broken = True
            await settle(45.0)

            assert len(checks) == 4  # проверки идут по расписанию
            assert {event.trigger for event in unsaved} == {"interval"}
            assert (
                fake_driver.live.pages == 0
            )  # и делают своё дело: тёплая вкладка A закрыта по простою


# --- плановый перезапуск ---------------------------------------------------------------


async def test_browser_is_recycled_after_so_many_leases(fake_driver: FakeDriver) -> None:
    lifecycle = Lifecycle(healthcheck_interval=5.0)
    recycling = Recycling(browser_max_leases=3, browser_max_age=None, recycle_jitter=0.0)
    async with make_pool(fake_driver, lifecycle=lifecycle, recycling=recycling) as pool:
        for _ in range(3):
            async with pool.page(A):
                pass
        await settle(6.0)

        assert launches(fake_driver) == 2
        async with pool.page(A) as lease:
            assert lease.page.alive


async def test_browser_is_recycled_after_its_uptime(fake_driver: FakeDriver) -> None:
    lifecycle = Lifecycle(healthcheck_interval=5.0)
    recycling = Recycling(browser_max_leases=None, browser_max_age=100.0, recycle_jitter=0.0)
    async with make_pool(fake_driver, lifecycle=lifecycle, recycling=recycling) as pool:
        async with pool.page(A):
            pass
        await settle(106.0)

        assert launches(fake_driver) == 2


async def test_only_one_browser_is_recycled_at_a_time(fake_driver: FakeDriver) -> None:
    lifecycle = Lifecycle(healthcheck_interval=5.0)
    recycling = Recycling(browser_max_leases=None, browser_max_age=100.0, recycle_jitter=0.0)
    topology = Topology(browsers=2, pages_per_browser=4, min_browsers=2)
    async with make_pool(
        fake_driver, lifecycle=lifecycle, recycling=recycling, topology=topology
    ) as pool:
        fake_driver.faults.delay("close_browser", 3.0)
        await settle(106.0)

        recycling = [
            state for state in browser_states(pool).values() if state is not BrowserState.healthy
        ]
        assert len(recycling) <= 1


# --- запуск и простой ------------------------------------------------------------------


async def test_min_browsers_are_started_up_front(fake_driver: FakeDriver) -> None:
    topology = Topology(browsers=3, pages_per_browser=4, min_browsers=2)
    async with make_pool(fake_driver, topology=topology):
        assert launches(fake_driver) == 2


async def test_launches_are_spaced_out(fake_driver: FakeDriver) -> None:
    topology = Topology(browsers=2, pages_per_browser=4, min_browsers=2)
    moments: list[float] = []
    original = fake_driver.launch

    async def spy(spec: object) -> FakeBrowser:
        moments.append(asyncio.get_running_loop().time())
        return await original(spec)  # pyright: ignore[reportArgumentType] — прокси к оригиналу

    fake_driver.launch = spy  # подмена для замера моментов запуска
    async with make_pool(fake_driver, topology=topology, limits=Limits(spawn_delay=3.0)):
        assert moments[1] - moments[0] == pytest.approx(3.0)


async def test_idle_browser_is_closed_and_comes_back_on_demand(fake_driver: FakeDriver) -> None:
    lifecycle = Lifecycle(
        browser_idle_ttl=30.0, context_idle_ttl=20.0, page_idle_ttl=20.0, healthcheck_interval=5.0
    )
    recycling = Recycling(browser_max_leases=None, browser_max_age=None)
    topology = Topology(browsers=1, pages_per_browser=4)
    async with make_pool(
        fake_driver, lifecycle=lifecycle, recycling=recycling, topology=topology
    ) as pool:
        async with pool.page(A):
            pass
        await settle(70.0)

        assert fake_driver.live == (0, 0, 0)
        assert browser_states(pool) == {"browser-0": BrowserState.healthy}
        async with pool.page(A) as lease:
            assert lease.page.alive
        assert launches(fake_driver) == 2


def browser_states(pool: BrowserPool[Any, Any, Any]) -> dict[str, BrowserState]:
    """Состояние каждого браузера — из снимка пула."""
    return {browser.id: browser.state for browser in pool.snapshot().browsers}
