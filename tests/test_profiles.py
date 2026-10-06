"""Профили на диске: свой браузер, готовый контекст, замок, образец, recycle."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig, ProxyPolicy
from browser_pool.clock import utc_now
from browser_pool.config import (
    Lifecycle,
    Limits,
    Recycling,
    Timeouts,
    Topology,
)
from browser_pool.errors import IdentityBusyError
from browser_pool.identity import StatePolicy
from browser_pool.locks import FileLock
from browser_pool.proxies import Proxy, ProxyList
from browser_pool.state import Cookie
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

SHARED = Identity(key="mail:shared")
P1 = Proxy(host="10.0.0.1", port=8080, id="p1")


def profiled(key: str, directory: Path, *, template: Path | None = None) -> Identity:
    return Identity(
        key=key,
        proxy=ProxyPolicy.sticky(),
        state=StatePolicy(mode="none", user_data_dir=directory, profile_template=template),
    )


def make_pool(
    driver: FakeDriver,
    *,
    browsers: int = 2,
    proxies: ProxyList | None = None,
    lifecycle: Lifecycle | None = None,
    recycling: Recycling | None = None,
    open_timeout: float = 30.0,
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=browsers, pages_per_browser=4, contexts_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            lifecycle=lifecycle or Lifecycle(),
            recycling=recycling or Recycling(browser_max_leases=None, browser_max_age=None),
            timeouts=Timeouts(open=open_timeout),
        ),
        proxy_source=proxies,
    )


def launches(driver: FakeDriver) -> list[FakeBrowser]:
    return [browser for browser in driver.browsers if browser.spec is not None]


async def is_free(directory: Path) -> bool:
    lock = FileLock(directory.with_name(f"{directory.name}.lock"))
    try:
        async with asyncio.timeout(0.5):
            await lock.acquire()
    except TimeoutError:
        return False
    lock.release()
    return True


async def freed_eventually(directory: Path, seconds: float = 10.0) -> bool:
    """В настоящем времени: Windows снимает замок убитого процесса не мгновенно."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        if await is_free(directory):
            return True
    return False


# --- размещение -------------------------------------------------------------------------


async def test_profile_gets_own_browser_and_its_ready_context(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    async with (
        make_pool(fake_driver) as pool,
        pool.page(identity) as own,
        pool.page(SHARED) as shared,
    ):
        assert own.browser is not shared.browser
        assert own.browser.spec is not None
        assert own.browser.spec.user_data_dir == tmp_path / "a"
        assert own.context.default  # готовый контекст профиля, а не новый
        assert shared.browser.spec is not None
        assert shared.browser.spec.user_data_dir is None
        assert not shared.context.default


async def test_profile_proxy_is_given_at_launch(fake_driver: FakeDriver, tmp_path: Path) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    sticky_shared = Identity(key="mail:shared", proxy=ProxyPolicy.sticky())
    async with (
        make_pool(fake_driver, proxies=ProxyList([P1])) as pool,
        pool.page(identity) as own,
        pool.page(sticky_shared) as shared,
    ):
        assert own.browser.spec is not None
        assert own.browser.spec.proxy == P1
        assert own.context.spec.proxy is None
        assert own.proxy == P1
        assert shared.browser.spec is not None
        assert shared.browser.spec.proxy is None  # общий браузер — прокси на контексте
        assert shared.context.spec.proxy == P1


async def test_slot_is_relaunched_between_profile_and_shared(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    async with make_pool(fake_driver, browsers=1) as pool:
        async with pool.page(identity):
            pass
        assert not await is_free(tmp_path / "a")  # браузер профиля жив — профиль занят

        async with pool.page(SHARED) as shared:
            spec = shared.browser.spec
            assert spec is not None
            assert spec.user_data_dir is None
        assert await is_free(tmp_path / "a")  # браузер профиля закрыт под общий

        async with pool.page(identity) as again:
            assert again.browser.spec is not None
            assert again.browser.spec.user_data_dir == tmp_path / "a"
    assert len(launches(fake_driver)) == 3


# --- профиль на диске -------------------------------------------------------------------


async def test_login_survives_in_profile_without_state_store(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    # Со сроком: сессионную куку профиль между запусками не хранит — как настоящий Chrome.
    cookie = Cookie(
        name="sid", value="42", domain="mail.test", expires=utc_now() + timedelta(days=30)
    )
    async with make_pool(fake_driver) as pool, pool.page(identity) as lease:
        await fake_driver.add_cookies(lease.context, [cookie])

    async with make_pool(fake_driver) as pool, pool.page(identity) as lease:
        assert lease.browser is not fake_driver.browsers[0]
        assert cookie in (await fake_driver.export_state(lease.context)).cookies


async def test_template_is_copied_only_into_missing_profile(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    template = tmp_path / "template"
    (template / "Default").mkdir(parents=True)
    (template / "Default" / "Preferences").write_text("template", encoding="utf-8")
    profile = tmp_path / "profiles" / "a"
    identity = profiled("mail:a", profile, template=template)

    async with make_pool(fake_driver) as pool, pool.page(identity):
        pass
    preferences = profile / "Default" / "Preferences"
    assert preferences.read_text(encoding="utf-8") == "template"

    preferences.write_text("used", encoding="utf-8")
    async with make_pool(fake_driver) as pool, pool.page(identity):
        pass
    assert preferences.read_text(encoding="utf-8") == "used"  # живой профиль не перезаписан


def test_template_without_profile_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="user_data_dir"):
        StatePolicy(profile_template=tmp_path)


# --- эксклюзивность ---------------------------------------------------------------------


async def test_profile_is_locked_for_other_pools_while_its_browser_lives(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    first = make_pool(fake_driver)
    second = make_pool(fake_driver, open_timeout=5.0)  # свой замок identity: как другой процесс

    async with first, second:
        async with first.page(identity):
            pass
        with pytest.raises(IdentityBusyError) as caught:
            async with second.page(identity, wait_cooldown=False):
                pass
        assert caught.value.identity == "mail:a"

    assert await is_free(tmp_path / "a")  # пулы остановлены — профиль свободен


async def test_two_identities_cannot_share_one_profile(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    first = profiled("mail:a", tmp_path / "shared")
    second = profiled("mail:b", tmp_path / "shared")
    async with make_pool(fake_driver, open_timeout=5.0) as pool, pool.page(first):
        with pytest.raises(IdentityBusyError):
            async with pool.page(second, wait_cooldown=False):
                pass


WORKER = """
import asyncio, sys, time
from pathlib import Path
from browser_pool import BrowserPool, Identity, StatePolicy
from browser_pool.testing import FakeDriver

async def main():
    identity = Identity(key="mail:a", state=StatePolicy(mode="none", user_data_dir=Path(sys.argv[1])))
    pool = BrowserPool(FakeDriver())
    await pool.start()
    async with pool.page(identity):
        print("leased", flush=True)
        time.sleep(60)

asyncio.run(main())
"""


async def test_crashed_worker_frees_the_profile(tmp_path: Path) -> None:
    # Приёмка M4: воркер с арендой профиля убит — профиль свободен без уборки и сроков.
    profile = tmp_path / "a"
    worker = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-c", WORKER, str(profile)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = await asyncio.to_thread(worker.stdout.readline) if worker.stdout else ""
        assert line.strip() == "leased"
        assert not await is_free(profile)

        worker.kill()
        await asyncio.to_thread(worker.wait)
        assert await freed_eventually(profile)
    finally:
        worker.kill()
        await asyncio.to_thread(worker.wait)
        if worker.stdout is not None:
            worker.stdout.close()


async def test_failed_launch_frees_the_profile(fake_driver: FakeDriver, tmp_path: Path) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    fake_driver.faults.fail("launch", RuntimeError("не запустился"))
    async with make_pool(fake_driver) as pool:
        with pytest.raises(RuntimeError, match="не запустился"):
            async with pool.page(identity, wait_cooldown=False):
                pass
        assert await is_free(tmp_path / "a")


# --- плановый перезапуск ----------------------------------------------------------------


async def test_profile_browser_is_not_recycled_by_leases_by_default(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    lifecycle = Lifecycle(healthcheck_interval=5.0)
    recycling = Recycling(browser_max_leases=2, browser_max_age=None, recycle_jitter=0.0)
    async with make_pool(fake_driver, lifecycle=lifecycle, recycling=recycling) as pool:
        for _ in range(4):
            async with pool.page(identity):
                pass
            await asyncio.sleep(6.0)
        profile_launches = [
            browser
            for browser in launches(fake_driver)
            if browser.spec is not None and browser.spec.user_data_dir is not None
        ]
        assert len(profile_launches) == 1

        for _ in range(4):
            async with pool.page(SHARED):
                pass
            await asyncio.sleep(6.0)
        shared_launches = [
            browser
            for browser in launches(fake_driver)
            if browser.spec is not None and browser.spec.user_data_dir is None
        ]
        assert len(shared_launches) > 1


async def test_profile_browser_recycles_when_asked(fake_driver: FakeDriver, tmp_path: Path) -> None:
    identity = profiled("mail:a", tmp_path / "a")
    lifecycle = Lifecycle(healthcheck_interval=5.0)
    recycling = Recycling(
        browser_max_leases=None,
        persistent_browser_max_leases=2,
        browser_max_age=None,
        recycle_jitter=0.0,
    )
    async with make_pool(fake_driver, lifecycle=lifecycle, recycling=recycling) as pool:
        for _ in range(4):
            async with pool.page(identity):
                pass
            await asyncio.sleep(6.0)
    assert len(launches(fake_driver)) > 1
