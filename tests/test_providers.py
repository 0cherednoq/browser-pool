"""Провайдер эндпоинтов: `start → attach`, `stop` ровно один раз, сироты, браузер владельца."""

from __future__ import annotations

import asyncio

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Backoff, Lifecycle, Limits, Recovery, Recycling, Timeouts, Topology
from browser_pool.driver import DriverCapabilities
from browser_pool.events import OrphansReaped
from browser_pool.testing import (
    FakeBrowser,
    FakeContext,
    FakeDriver,
    FakeDriverError,
    FakeEndpointProvider,
    FakePage,
)

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

A = Identity(key="mail:a")
B = Identity(key="mail:b")
VENDOR = DriverCapabilities(
    proxy_scope="external",
    can_new_context=False,
    fingerprint_scope="external",
    state_support="cookies",
)
"""Антидетект: браузер — профиль вендора, одна identity на браузер."""


def make_pool(
    driver: FakeDriver, provider: FakeEndpointProvider, *, browsers: int = 1, min_browsers: int = 0
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=browsers, min_browsers=min_browsers, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            lifecycle=Lifecycle(healthcheck_interval=10.0),
            recycling=Recycling(recycle_jitter=0.0),
            recovery=Recovery(
                restart_backoff=Backoff(initial=1.0, maximum=60.0, factor=2.0, jitter=0.0)
            ),
            timeouts=Timeouts(close=2.0, restart=5.0, startup=5.0),
        ),
        provider=provider,
    )


def operations(driver: FakeDriver, name: str) -> int:
    return sum(call.operation == name for call in driver.calls)


async def test_browser_comes_from_the_provider(fake_driver: FakeDriver) -> None:
    provider = FakeEndpointProvider()

    async with make_pool(fake_driver, provider) as pool, pool.page(A) as lease:
        assert lease.browser.endpoint is not None
        assert lease.browser.endpoint.url == "fake://endpoint/1"
        (request,) = provider.requests
        assert request.browser_id == "browser-0"
        assert request.identity is None  # браузер общий: владельца нет
        assert request.spec.headless

    assert operations(fake_driver, "launch") == 0
    assert operations(fake_driver, "attach") == 1
    assert [endpoint.url for endpoint in provider.stopped] == ["fake://endpoint/1"]
    assert provider.active == ()


async def test_endpoint_is_stopped_when_attach_fails(fake_driver: FakeDriver) -> None:
    provider = FakeEndpointProvider()
    fake_driver.faults.fail("attach", FakeDriverError("не подключились"))

    async with make_pool(fake_driver, provider) as pool:
        with pytest.raises(FakeDriverError):
            async with pool.page(A):
                pass
        assert [endpoint.url for endpoint in provider.stopped] == ["fake://endpoint/1"]
        pool.unblock(A.key)  # снять паузу после неудачного открытия

        async with pool.page(A) as lease:
            assert lease.page.alive

    assert provider.active == ()
    assert provider.double_stops == 0


async def test_endpoint_is_stopped_when_a_browser_hook_fails(fake_driver: FakeDriver) -> None:
    provider = FakeEndpointProvider()
    pool = make_pool(fake_driver, provider)

    @pool.hooks.after_browser_started
    async def broken(browser: FakeBrowser, browser_id: str) -> None:
        _ = browser, browser_id
        msg = "хук упал"
        raise RuntimeError(msg)

    async with pool:
        with pytest.raises(RuntimeError, match="хук упал"):
            async with pool.page(A):
                pass

    assert len(provider.stopped) == 1
    assert provider.active == ()


async def test_crashed_browser_gives_its_endpoint_back_once(fake_driver: FakeDriver) -> None:
    provider = FakeEndpointProvider()

    async with make_pool(fake_driver, provider) as pool:
        async with pool.page(A) as lease:
            crashed = lease.browser
        fake_driver.crash(crashed)
        await asyncio.sleep(1.0)
        async with pool.page(A) as lease:
            assert lease.browser is not crashed

    assert crashed.endpoint is not None
    assert provider.stopped[0] == crashed.endpoint  # старый освобождён до нового
    assert provider.active == ()
    assert provider.double_stops == 0


async def test_orphans_of_the_provider_are_reaped_at_start(fake_driver: FakeDriver) -> None:
    provider = FakeEndpointProvider(orphans=3)
    events: list[OrphansReaped] = []
    pool = make_pool(fake_driver, provider)
    pool.on(OrphansReaped, events.append)

    async with pool:
        await asyncio.sleep(0)

    assert [event.count for event in events] == [3]
    assert provider.orphans == 0


async def test_failing_stop_is_counted_and_does_not_break_the_pool(fake_driver: FakeDriver) -> None:
    provider = FakeEndpointProvider()
    provider.stop_error = RuntimeError("API вендора недоступен")

    async with make_pool(fake_driver, provider) as pool:
        async with pool.page(A):
            pass
        await pool.stop()
        assert pool.snapshot().counters.close_failures == 1

    assert provider.active == ()


async def test_vendor_profile_is_started_for_its_identity(fake_driver: FakeDriver) -> None:
    driver = FakeDriver(capabilities=VENDOR)
    provider = FakeEndpointProvider()

    async with make_pool(driver, provider, min_browsers=1) as pool:
        assert provider.requests == []  # профиль впрок не поднять: нет identity
        async with pool.page(A):
            pass
        async with pool.page(B):
            pass

    assert [request.identity for request in provider.requests] == [A, B]
    assert provider.active == ()
    assert provider.double_stops == 0  # профиль A освобождён, когда браузер отдали B
    assert tuple(driver.live) == (0, 0, 0)


async def test_every_endpoint_is_stopped_exactly_once_under_load() -> None:
    driver = FakeDriver()
    provider = FakeEndpointProvider()
    identities = [Identity(key=f"mail:{index}") for index in range(6)]

    async def work(identity: Identity) -> None:
        async with pool.page(identity):
            await asyncio.sleep(0.1)

    pool = make_pool(driver, provider, browsers=2)
    async with pool:
        await asyncio.gather(*(work(identity) for identity in identities))
        driver.crash(driver.browsers[0])
        await asyncio.sleep(2.0)
        await asyncio.gather(*(work(identity) for identity in identities))

    assert len(provider.requests) == len(provider.stopped)
    assert provider.active == ()
    assert provider.double_stops == 0
