"""Отладочные опции: вкладка после ошибки остаётся открытой, подпись окна, параметры запуска."""

from __future__ import annotations

import asyncio

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig, clock
from browser_pool.config import Debug, Limits, Recycling, Topology
from browser_pool.proxies import Proxy, ProxyList
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

A = Identity(key="mail:a")

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


class SiteBrokeError(Exception):
    """Сбой сайта посреди аренды."""


def make_pool(driver: FakeDriver, debug: Debug, **kwargs: object) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=2),
            limits=Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            debug=debug,
        ),
        **kwargs,  # pyright: ignore[reportArgumentType] — прокси и прочее
    )


async def broken_lease(pool: Pool, pages: list[FakePage]) -> None:
    async with pool.page(A) as lease:
        pages.append(lease.page)
        raise SiteBrokeError


async def fail_in(pool: Pool) -> FakePage:
    pages: list[FakePage] = []
    with pytest.raises(SiteBrokeError):
        await broken_lease(pool, pages)
    return pages[0]


# --- hold_on_error ---------------------------------------------------------------------


async def test_failed_page_stays_open_and_keeps_its_slot(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, Debug(hold_on_error=60.0)) as pool:
        started = clock.monotonic()
        page = await fail_in(pool)  # исключение пришло сразу, без ожидания
        assert clock.monotonic() == started

        await asyncio.sleep(30)
        assert page.alive
        assert pool.held_pages.held == 1
        assert pool.snapshot().leases_active == 1  # слот честно занят

        await asyncio.sleep(31)
        assert not page.alive  # вкладка после ошибки закрывается, как обычно
        assert pool.snapshot().leases_active == 0


async def test_release_lets_held_pages_go_at_once(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, Debug(hold_on_error=600.0)) as pool:
        page = await fail_in(pool)
        await asyncio.sleep(1)

        assert pool.held_pages.release() == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not page.alive
        assert pool.held_pages.held == 0


async def test_stop_does_not_wait_for_held_pages(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver, Debug(hold_on_error=3600.0))
    await pool.start()
    page = await fail_in(pool)
    started = clock.monotonic()

    await pool.stop()

    assert clock.monotonic() - started < 60
    assert not page.alive


async def test_without_hold_the_failed_page_closes_at_once(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, Debug()) as pool:
        page = await fail_in(pool)
        await asyncio.sleep(0)

        assert not page.alive
        assert pool.held_pages.held == 0


# --- подпись окна и запуск -------------------------------------------------------------


async def test_window_label_names_the_identity_and_its_proxy(fake_driver: FakeDriver) -> None:
    proxies = ProxyList([Proxy(host="10.0.0.7", port=8080, id="proxy#7")])

    async with (
        make_pool(fake_driver, Debug(label_windows=True), proxy_source=proxies) as pool,
        pool.page(A) as lease,
    ):
        assert lease.page.label == "[mail:a · proxy#7] "


async def test_window_label_is_off_by_default(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, Debug()) as pool, pool.page(A) as lease:
        assert lease.page.label is None


async def test_launch_gets_slow_mo_and_background_flags(fake_driver: FakeDriver) -> None:
    debug = Debug(slow_mo=150.0, keep_background_active=True)

    async with make_pool(fake_driver, debug) as pool, pool.page(A):
        pass

    (browser,) = fake_driver.browsers
    assert browser.spec is not None
    assert browser.spec.slow_mo == 150.0
    assert browser.spec.keep_background_active
