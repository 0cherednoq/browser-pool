"""Изменение пула на лету: `pool.resize`, `pool.reconfigure`."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Lifecycle, Limits, Recycling, Topology
from browser_pool.errors import ConfigError, PoolSaturatedError
from browser_pool.snapshot import BrowserState
from browser_pool.testing import FAKE_CAPABILITIES, FakeBrowser, FakeContext, FakeDriver, FakePage

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

IDENTITIES = [Identity(key=f"mail:{index}") for index in range(6)]
A, B, C = IDENTITIES[:3]


def make_pool(driver: FakeDriver, **topology: int) -> Pool:
    settings = {"browsers": 1, "pages_per_browser": 1, "warm_pages_per_identity": 0, **topology}
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(**settings),
            limits=Limits(spawn_delay=0.0),
            lifecycle=Lifecycle(healthcheck_interval=30.0),
            recycling=Recycling(browser_max_leases=None),
        ),
    )


class Holder:
    """Держит аренды, пока не отпустят."""

    def __init__(self, pool: Pool) -> None:
        self.pool = pool
        self.release = asyncio.Event()
        self.browsers: dict[str, str] = {}

    async def hold(self, identity: Identity) -> None:
        async with self.pool.page(identity) as lease:
            self.browsers[identity.key] = lease.browser_id
            await self.release.wait()

    def start(self, *identities: Identity) -> list[asyncio.Task[None]]:
        return [asyncio.create_task(self.hold(identity)) for identity in identities]


def slots(pool: Pool) -> list[str]:
    return [browser.id for browser in pool.snapshot().browsers]


async def test_growing_browsers_takes_effect_at_once(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        holder = Holder(pool)
        tasks = holder.start(A, B)
        await asyncio.sleep(0.1)
        assert pool.snapshot().waiting == 1

        await pool.resize(browsers=2)
        await asyncio.sleep(0.1)
        assert pool.snapshot().leases_active == 2
        assert slots(pool) == ["browser-0", "browser-1"]
        assert pool.config.topology.browsers == 2
        holder.release.set()
        await asyncio.gather(*tasks)


async def test_shrinking_drains_the_emptiest_browser(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, browsers=2) as pool:
        holder = Holder(pool)
        tasks = holder.start(A)
        await asyncio.sleep(0.1)
        busy = holder.browsers["mail:0"]

        await pool.resize(browsers=1)  # пустой слот убирается сразу

        assert slots(pool) == [busy]
        holder.release.set()
        await asyncio.gather(*tasks)


async def test_shrinking_waits_for_leases_and_never_cuts_them(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, browsers=2) as pool:
        holder = Holder(pool)
        tasks = holder.start(A, B)
        await asyncio.sleep(0.1)

        shrink = asyncio.create_task(pool.resize(browsers=1))
        await asyncio.sleep(1.0)
        assert not shrink.done()  # ждёт, пока вернут аренду
        assert pool.snapshot().leases_active == 2
        assert sorted(state.value for state in browser_states(pool).values()) == [
            "draining",
            "healthy",
        ]

        holder.release.set()
        await asyncio.gather(*tasks)
        await asyncio.wait_for(shrink, timeout=5)
        assert len(slots(pool)) == 1
        assert fake_driver.live.browsers <= 1


async def test_new_slots_reuse_the_lowest_free_numbers(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, browsers=3) as pool:
        await pool.resize(browsers=1)
        assert slots(pool) == ["browser-0"]

        await pool.resize(browsers=3)
        assert sorted(slots(pool)) == ["browser-0", "browser-1", "browser-2"]


async def test_more_pages_per_browser_serve_more_leases(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        holder = Holder(pool)
        tasks = holder.start(A, B, C)
        await asyncio.sleep(0.1)
        assert pool.snapshot().leases_active == 1

        await pool.resize(pages_per_browser=3)
        await asyncio.sleep(0.1)
        assert pool.snapshot().leases_active == 3
        holder.release.set()
        await asyncio.gather(*tasks)


async def test_fewer_pages_per_browser_do_not_take_leases_away(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, pages_per_browser=3) as pool:
        holder = Holder(pool)
        tasks = holder.start(A, B)
        await asyncio.sleep(0.1)

        await pool.resize(pages_per_browser=1)
        late = holder.start(C)
        await asyncio.sleep(0.1)
        assert pool.snapshot().leases_active == 2  # выданные остались
        assert pool.snapshot().waiting == 1  # новой места нет
        holder.release.set()
        await asyncio.gather(*tasks, *late)


async def test_driver_hint_still_caps_pages_after_resize() -> None:
    driver = FakeDriver(capabilities=dataclasses.replace(FAKE_CAPABILITIES, max_pages_hint=2))
    async with make_pool(driver) as pool:
        await pool.resize(pages_per_browser=8)

        assert pool.config.topology.pages_per_browser == 8
        assert pool.effective_config.topology.pages_per_browser == 2


async def test_incompatible_size_is_rejected_and_nothing_changes(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, browsers=2, min_browsers=2) as pool:
        with pytest.raises(ConfigError, match="min_browsers"):
            await pool.resize(browsers=1)

        assert len(slots(pool)) == 2
        assert pool.config.topology.browsers == 2


async def test_crash_while_draining_still_removes_the_slot(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, browsers=2) as pool:
        holder = Holder(pool)
        tasks = holder.start(A, B)
        await asyncio.sleep(0.1)
        shrink = asyncio.create_task(pool.resize(browsers=1))
        await asyncio.sleep(0.1)

        fake_driver.crash(fake_driver.browsers[0])
        fake_driver.crash(fake_driver.browsers[1])
        holder.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(shrink, timeout=10)

        assert len(slots(pool)) == 1


# --- reconfigure -----------------------------------------------------------------------


async def test_reconfigured_limits_apply_to_the_next_requests(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        holder = Holder(pool)
        tasks = holder.start(A, B)
        await asyncio.sleep(0.1)

        pool.reconfigure(limits=Limits(spawn_delay=0.0, max_waiting=1))
        with pytest.raises(PoolSaturatedError):
            async with pool.page(C):
                pass

        assert pool.config.limits.max_waiting == 1
        holder.release.set()
        await asyncio.gather(*tasks)


async def test_reconfigured_lifecycle_wears_contexts_out_sooner(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            first = lease.generation
        pool.reconfigure(
            lifecycle=Lifecycle(healthcheck_interval=30.0),
            recycling=Recycling(context_max_leases=1),
        )
        async with pool.page(A):  # вторая аренда контекста — порог 1 уже пройден
            pass
        await asyncio.sleep(0.1)
        async with pool.page(A) as lease:
            assert lease.generation > first


def browser_states(pool: BrowserPool[Any, Any, Any]) -> dict[str, BrowserState]:
    """Состояние каждого браузера — из снимка пула."""
    return {browser.id: browser.state for browser in pool.snapshot().browsers}
