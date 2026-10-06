"""Хуки: точки жизненного цикла, порядок, плагины, и никаких ресурсов, брошенных после сбоя."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from browser_pool import BrowserPool, Identity, PageLease, PoolConfig
from browser_pool.config import Limits, Recycling, Topology
from browser_pool.driver import ContextSpec, LaunchSpec
from browser_pool.errors import ConfigError
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

A = Identity(key="mail:a")

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]
type Lease = PageLease[FakeBrowser, FakeContext, FakePage, Any]


def make_pool(driver: FakeDriver, *, plugins: tuple[object, ...] = ()) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
        ),
        plugins=plugins,
    )


def calls(driver: FakeDriver, operation: str) -> int:
    return sum(call.operation == operation for call in driver.calls)


class Stealth:
    """Плагин: подмножество хуков, остальные не нужны."""

    def __init__(self, journal: list[str]) -> None:
        self.journal = journal

    def before_launch(self, spec: LaunchSpec, browser_id: str) -> None:
        spec.args.append("--disable-blink-features=AutomationControlled")
        self.journal.append(f"plugin before_launch {browser_id}")

    async def after_page_created(self, page: FakePage, identity: Identity) -> None:
        await asyncio.sleep(0)
        self.journal.append(f"plugin after_page_created {identity.key} {page.id}")


class Typo:
    async def after_context_creatd(self, context: FakeContext, identity: Identity) -> None:
        _ = context, identity


class OldNames:
    def pre_launch(self, spec: LaunchSpec, browser_id: str) -> None:
        _ = spec, browser_id


class Idle:
    def helper(self) -> None:
        """Ни одной точки."""


def test_plugin_with_a_mistyped_hook_name_is_rejected(fake_driver: FakeDriver) -> None:
    with pytest.raises(ConfigError, match="after_context_created"):
        make_pool(fake_driver, plugins=(Typo(),))


def test_plugin_with_a_retired_hook_name_is_rejected(fake_driver: FakeDriver) -> None:
    with pytest.raises(ConfigError, match="before_launch"):
        make_pool(fake_driver, plugins=(OldNames(),))


def test_plugin_without_any_hook_is_rejected(fake_driver: FakeDriver) -> None:
    with pytest.raises(ConfigError, match="ни одной точки"):
        make_pool(fake_driver, plugins=(Idle(),))


def test_hooks_are_typed_by_pool_generics(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)

    @pool.hooks.after_page_created
    async def label(page: FakePage, identity: Identity) -> None:
        _ = page.id, identity.key  # pyright проверяет: page — FakePage

    assert label is not None


# --- спецификации и порядок ------------------------------------------------------------


async def test_hooks_shape_specs_in_registration_order(fake_driver: FakeDriver) -> None:
    journal: list[str] = []
    pool = make_pool(fake_driver, plugins=(Stealth(journal),))

    @pool.hooks.before_launch
    def headed(spec: LaunchSpec, browser_id: str) -> None:
        spec.headless = False
        journal.append(f"decorator before_launch {browser_id}")

    @pool.hooks.before_context
    async def russian(spec: ContextSpec, identity: Identity) -> None:
        spec.locale = "ru-RU"
        journal.append(f"before_context {identity.key}")

    async with pool, pool.page(A) as lease:
        assert lease.browser.spec is not None
        assert lease.browser.spec.args == ["--disable-blink-features=AutomationControlled"]
        assert lease.browser.spec.headless is False
        assert lease.context.spec.locale == "ru-RU"

    assert journal[:3] == [
        "plugin before_launch browser-0",
        "decorator before_launch browser-0",
        "before_context mail:a",
    ]


async def test_resource_hooks_run_once_per_resource(fake_driver: FakeDriver) -> None:
    journal: list[str] = []
    pool = make_pool(fake_driver, plugins=(Stealth(journal),))

    @pool.hooks.after_browser_started
    def browser_seen(browser: FakeBrowser, browser_id: str) -> None:
        journal.append(f"after_browser_started {browser_id}")

    @pool.hooks.after_context_created
    def context_seen(context: FakeContext, identity: Identity) -> None:
        journal.append(f"after_context_created {identity.key}")

    async with pool:
        for _ in range(3):
            async with pool.page(A):
                pass

    assert journal.count("after_browser_started browser-0") == 1
    assert journal.count("after_context_created mail:a") == 1
    assert (
        sum(entry.startswith("plugin after_page_created") for entry in journal) == 1
    )  # вкладка тёплая


async def test_lease_hooks_run_around_every_lease(fake_driver: FakeDriver) -> None:
    journal: list[str] = []
    pool = make_pool(fake_driver)

    @pool.hooks.after_acquire
    def acquired(lease: Lease) -> None:
        journal.append(f"acquire {lease.lease_id}")

    @pool.hooks.before_release
    async def released(lease: Lease) -> None:
        journal.append(f"release {lease.lease_id}")

    async with pool:
        for _ in range(2):
            async with pool.page(A) as lease:
                journal.append(f"work {lease.lease_id}")

    assert journal == ["acquire 1", "work 1", "release 1", "acquire 2", "work 2", "release 2"]


# --- сбой хука не оставляет ресурсов ---------------------------------------------------


async def test_failing_browser_hook_leaves_no_live_browser(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)

    @pool.hooks.after_browser_started
    def broken(browser: FakeBrowser, browser_id: str) -> None:
        msg = "расширение не встало"
        raise RuntimeError(msg)

    async with pool:
        with pytest.raises(RuntimeError, match="расширение"):
            async with pool.page(A):
                pass
        assert fake_driver.live.browsers == 0


async def test_failing_context_hook_closes_the_context(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)

    @pool.hooks.after_context_created
    def broken(context: FakeContext, identity: Identity) -> None:
        msg = "отпечаток не встал"
        raise RuntimeError(msg)

    async with pool:
        with pytest.raises(RuntimeError, match="отпечаток"):
            async with pool.page(A):
                pass
        assert fake_driver.live.contexts == 0
        assert pool.identity_status("mail:a").open_failures == 1


async def test_failing_page_hook_closes_the_page(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)

    @pool.hooks.after_page_created
    def broken(page: FakePage, identity: Identity) -> None:
        msg = "блокировщик не встал"
        raise RuntimeError(msg)

    async with pool:
        with pytest.raises(RuntimeError, match="блокировщик"):
            async with pool.page(A):
                pass
        assert fake_driver.live.pages == 0


async def test_failing_acquire_hook_keeps_the_tenant_out(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    entered: list[bool] = []

    @pool.hooks.after_acquire
    def broken(lease: Lease) -> None:
        msg = "прогрев упал"
        raise RuntimeError(msg)

    async def tenant() -> None:
        async with pool.page(A):
            entered.append(True)

    async with pool:
        with pytest.raises(RuntimeError, match="прогрев"):
            await tenant()

        assert entered == []
        assert fake_driver.live.pages == 0


async def test_failing_release_hook_is_logged_and_the_page_discarded(
    fake_driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    pool = make_pool(fake_driver)

    @pool.hooks.before_release
    def broken(lease: Lease) -> None:
        msg = "сброс упал"
        raise RuntimeError(msg)

    with caplog.at_level(logging.WARNING, logger="browser_pool"):
        async with pool:
            async with pool.page(A) as lease:
                page = lease.page
            assert not page.alive

    assert "before_release" in caplog.text


async def test_plugin_methods_outside_the_hook_namespace_are_ignored(
    fake_driver: FakeDriver,
) -> None:
    class Partial:
        def before_launch(self, spec: LaunchSpec, browser_id: str) -> None:
            _ = spec, browser_id

        def something_else(self) -> None:
            raise AssertionError

    async with make_pool(fake_driver, plugins=(Partial(),)) as pool, pool.page(A) as lease:
        assert lease.page.alive
    assert calls(fake_driver, "launch") == 1
