"""Защита хоста (`Resources`): давление памяти и CPU, `shrink`, распухший браузер."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import override

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Lifecycle, Limits, Recycling, Resources, Topology
from browser_pool.errors import ConfigError
from browser_pool.events import BrowserRecycled, ResourcePressure
from browser_pool.host import HostProbe
from browser_pool.procguard import ProcessGuard
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakeHostProbe, FakePage

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

A = Identity(key="mail:a")
B = Identity(key="mail:b")
CHECK = 1.0
"""Период проверки здоровья в тестах, секунды (виртуальные)."""


class InertGuard(ProcessGuard):
    """Страж, который никого не берёт на учёт: у фейковых браузеров выдуманные PID."""

    @override
    async def track(self, pid: int) -> None:
        _ = pid

    @override
    async def release(self, pid: int, *, grace: float) -> bool:
        _ = pid, grace
        return False

    @override
    async def reap_orphans(self) -> int:
        return 0


class NumberedDriver(FakeDriver):
    """Фейк, у браузеров которого есть «процесс» — номер по порядку запуска."""

    @override
    def pid(self, browser: FakeBrowser) -> int | None:
        return 900_000 + self.browsers.index(browser)


def make_pool(
    driver: FakeDriver,
    probe: HostProbe | None,
    resources: Resources,
    *,
    browsers: int = 1,
    min_browsers: int = 0,
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=browsers, min_browsers=min_browsers, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            lifecycle=Lifecycle(healthcheck_interval=CHECK),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            resources=resources,
        ),
        host_probe=probe,
        process_guard=InertGuard(),
    )


async def checked() -> None:
    """Дождаться проверки здоровья."""
    await asyncio.sleep(CHECK * 1.5)


async def _use(pool: Pool, identity: Identity) -> None:
    async with pool.page(identity):
        pass


async def test_memory_pressure_holds_new_contexts_but_serves_open_ones(
    fake_driver: FakeDriver,
) -> None:
    probe = FakeHostProbe()
    events: list[ResourcePressure] = []
    pool = make_pool(fake_driver, probe, Resources(min_free_memory_mb=1024))
    pool.on(ResourcePressure, events.append)

    async with pool:
        await _use(pool, A)
        probe.free_memory = 500
        await checked()
        assert pool.snapshot().under_pressure

        await _use(pool, A)  # открытый контекст — работает
        waiter = asyncio.create_task(_use(pool, B))
        await asyncio.sleep(0.1)
        assert pool.snapshot().waiting == 1  # новый контекст ждёт

        probe.free_memory = 4096
        await checked()
        await asyncio.wait_for(waiter, timeout=5)
        assert not pool.snapshot().under_pressure

    assert [event.reason is not None for event in events] == [True, False]
    assert "памяти" in (events[0].reason or "")
    assert events[0].free_memory_mb == 500


async def test_cpu_is_averaged_over_the_window(fake_driver: FakeDriver) -> None:
    probe = FakeHostProbe(cpu_percent=95)
    resources = Resources(max_cpu_percent=80, sample_window=CHECK * 3)

    async with make_pool(fake_driver, probe, resources) as pool:
        await checked()
        assert pool.snapshot().under_pressure
        probe.cpu = 70  # среднее с прошлыми 95 ещё выше потолка
        await asyncio.sleep(CHECK)
        assert pool.snapshot().under_pressure
        await asyncio.sleep(CHECK * 5)  # старые замеры вышли из окна
        assert not pool.snapshot().under_pressure


async def test_shrink_closes_idle_pages_contexts_and_browsers(fake_driver: FakeDriver) -> None:
    probe = FakeHostProbe()
    resources = Resources(min_free_memory_mb=1024, pressure_action="shrink")

    async with make_pool(fake_driver, probe, resources) as pool:
        await _use(pool, A)
        await _use(pool, B)
        assert fake_driver.live.pages == 2
        probe.free_memory = 100
        await asyncio.sleep(CHECK * 5)

        assert tuple(fake_driver.live) == (0, 0, 0)  # вкладки, контексты, браузер — закрыты
        assert pool.snapshot().contexts == ()


async def test_hold_keeps_what_is_open(fake_driver: FakeDriver) -> None:
    probe = FakeHostProbe()

    async with make_pool(fake_driver, probe, Resources(min_free_memory_mb=1024)) as pool:
        await _use(pool, A)
        probe.free_memory = 100
        await asyncio.sleep(CHECK * 4)

        assert fake_driver.live.contexts == 1


async def test_swollen_browser_is_recycled_with_drain() -> None:
    driver = NumberedDriver()
    probe = FakeHostProbe()
    recycled: list[BrowserRecycled] = []
    pool = make_pool(driver, probe, Resources(max_browser_rss_mb=900))
    pool.on(BrowserRecycled, recycled.append)

    async with pool:
        async with pool.page(A):
            probe.rss[900_000] = 1500
            await checked()
            assert driver.browsers[0].alive  # аренда дорабатывает — не убит
        await checked()
        async with pool.page(A) as lease:
            assert lease.browser is driver.browsers[1]

    assert [event.reason for event in recycled] == ["rss"]
    assert not driver.browsers[0].alive


async def test_shrink_keeps_min_browsers(fake_driver: FakeDriver) -> None:
    probe = FakeHostProbe(free_memory_mb=100)
    resources = Resources(min_free_memory_mb=1024, pressure_action="shrink")
    pool = make_pool(fake_driver, probe, resources, min_browsers=1)

    async with pool:
        await asyncio.sleep(CHECK * 5)

        assert fake_driver.live.browsers == 1  # минимум — явная гарантия, shrink его не трогает
        assert len(fake_driver.browsers) == 1


async def test_resources_without_a_probe_are_ignored_with_a_warning(
    fake_driver: FakeDriver, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("browser_pool._core.assembly.default_probe", lambda: None)

    with caplog.at_level(logging.WARNING):
        async with make_pool(fake_driver, None, Resources(min_free_memory_mb=1024)) as pool:
            await _use(pool, A)

    assert "resources игнорируется" in caplog.text


@pytest.mark.parametrize(
    "values",
    [{"min_free_memory_mb": 0.0}, {"max_cpu_percent": 150.0}, {"sample_window": -1.0}],
    ids=["memory", "cpu", "window"],
)
def test_bad_resources_are_rejected(values: dict[str, float]) -> None:
    with pytest.raises(ConfigError):
        Resources(**values)  # pyright: ignore[reportArgumentType] — числа порогов


def test_resources_section_round_trips() -> None:
    config = PoolConfig.from_mapping(
        {"resources": {"min_free_memory_mb": 512, "pressure_action": "shrink"}}
    )

    assert config.resources == Resources(min_free_memory_mb=512, pressure_action="shrink")
    assert PoolConfig.from_mapping(config.to_mapping()) == config


def test_psutil_probe_measures_this_process() -> None:
    pytest.importorskip("psutil")
    from browser_pool.monitors.psutil import PsutilProbe

    probe = PsutilProbe()

    assert probe.free_memory_mb() > 0
    assert 0 <= probe.cpu_percent() <= 100
    rss = probe.tree_rss_mb(os.getpid())
    assert rss is not None
    assert rss > 1
    assert probe.tree_rss_mb(2**22 + 7) is None
