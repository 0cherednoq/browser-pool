"""Фейковый драйвер: контракт, сценарные сбои, падения, учёт живых ресурсов."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from browser_pool import ErrorKind, clock
from browser_pool.clock import utc_now
from browser_pool.driver import ContextSpec, Driver, DriverCapabilities, Endpoint, LaunchSpec
from browser_pool.proxies import Proxy
from browser_pool.state import Cookie, SessionState
from browser_pool.testing import (
    FakeBrowser,
    FakeBrowserCrashedError,
    FakeContext,
    FakeDriver,
    FakeDriverError,
    FakePage,
    FakeTargetClosedError,
)
from browser_pool.testing.contract_site import ContractSite, safe_port

SID = Cookie(name="sid", value="a", domain=".example.com")


def test_fake_satisfies_the_driver_protocol() -> None:
    driver: Driver[FakeBrowser, FakeContext, FakePage] = FakeDriver()

    assert isinstance(driver, Driver)


async def test_full_lifecycle_is_journaled_and_leaves_nothing(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    context = await fake_driver.new_context(browser, ContextSpec())
    page = await fake_driver.new_page(context)

    assert fake_driver.live == (1, 1, 1)
    assert fake_driver.page_usable(page)

    await fake_driver.close_page(page)
    await fake_driver.close_context(context)
    await fake_driver.close_browser(browser)

    assert fake_driver.live == (0, 0, 0)
    assert [call.operation for call in fake_driver.calls] == [
        "launch",
        "new_context",
        "new_page",
        "close_page",
        "close_context",
        "close_browser",
    ]


async def test_closing_browser_closes_everything_inside(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    context = await fake_driver.new_context(browser, ContextSpec())
    page = await fake_driver.new_page(context)

    await fake_driver.close_browser(browser)

    assert fake_driver.live == (0, 0, 0)
    assert not fake_driver.page_usable(page)


async def test_attach_gives_a_browser_bound_to_the_endpoint(fake_driver: FakeDriver) -> None:
    endpoint = Endpoint(kind="cdp", url="ws://127.0.0.1:9222/devtools/browser/x", pid=77)

    browser = await fake_driver.attach(endpoint)

    assert browser.endpoint is endpoint
    assert fake_driver.pid(browser) == 77
    await fake_driver.close_browser(browser)


# --- состояние -------------------------------------------------------------------------


async def test_cookies_are_isolated_between_contexts(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    first = await fake_driver.new_context(browser, ContextSpec())
    second = await fake_driver.new_context(browser, ContextSpec())

    await fake_driver.add_cookies(first, [SID])

    assert (await fake_driver.export_state(first)).cookies == (SID,)
    assert (await fake_driver.export_state(second)).cookies == ()
    await fake_driver.close_browser(browser)


async def test_state_round_trips_through_a_new_context(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    source = await fake_driver.new_context(
        browser, ContextSpec(state=SessionState(cookies=(SID,), extras={"token": "t"}))
    )

    exported = await fake_driver.export_state(source)
    restored = await fake_driver.new_context(browser, ContextSpec(state=exported))

    assert await fake_driver.export_state(restored) == exported
    await fake_driver.close_browser(browser)


async def test_same_cookie_is_replaced_not_duplicated(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    context = await fake_driver.new_context(browser, ContextSpec())
    newer = Cookie(
        name="sid", value="b", domain=".example.com", expires=datetime(2030, 1, 1, tzinfo=UTC)
    )

    await fake_driver.add_cookies(context, [SID])
    await fake_driver.add_cookies(context, [newer])

    assert (await fake_driver.export_state(context)).cookies == (newer,)
    await fake_driver.close_browser(browser)


# --- сценарные сбои --------------------------------------------------------------------


async def test_scripted_failure_hits_the_next_calls_only(fake_driver: FakeDriver) -> None:
    fake_driver.faults.fail("launch", RuntimeError("нет бинаря"), times=2)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="нет бинаря"):
            await fake_driver.launch(LaunchSpec())
    browser = await fake_driver.launch(LaunchSpec())

    assert fake_driver.live == (1, 0, 0)
    await fake_driver.close_browser(browser)


async def test_scripted_hang_waits_until_cancelled(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    fake_driver.faults.hang("close_browser")
    started = clock.monotonic()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(10):
            await fake_driver.close_browser(browser)

    assert clock.monotonic() - started == pytest.approx(10)
    assert fake_driver.live == (1, 0, 0)  # зависший close ничего не закрыл
    await fake_driver.kill_browser(browser)


async def test_scripted_delay_takes_virtual_time(fake_driver: FakeDriver) -> None:
    fake_driver.faults.delay("launch", 7.0)
    started = clock.monotonic()

    browser = await fake_driver.launch(LaunchSpec())

    assert clock.monotonic() - started == pytest.approx(7)
    await fake_driver.close_browser(browser)


# --- падения ---------------------------------------------------------------------------


async def test_crash_notifies_and_kills_everything_inside(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    context = await fake_driver.new_context(browser, ContextSpec())
    page = await fake_driver.new_page(context)
    notified = asyncio.Event()
    fake_driver.on_disconnect(browser, notified.set)

    fake_driver.crash(browser)
    await asyncio.wait_for(notified.wait(), timeout=1)

    assert not await fake_driver.ping(browser)
    assert not fake_driver.page_usable(page)
    # Процесс упавшего браузера пул обязан прибрать сам: он ещё числится живым.
    assert fake_driver.live == (1, 0, 0)
    with pytest.raises(FakeBrowserCrashedError) as caught:
        await fake_driver.new_context(browser, ContextSpec())
    assert fake_driver.classify(caught.value) is ErrorKind.browser

    await fake_driver.close_browser(browser)
    assert fake_driver.live == (0, 0, 0)


async def test_closed_page_is_classified_as_page_failure(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    context = await fake_driver.new_context(browser, ContextSpec())
    await fake_driver.close_context(context)

    with pytest.raises(FakeTargetClosedError) as caught:
        await fake_driver.new_page(context)

    assert fake_driver.classify(caught.value) is ErrorKind.page
    assert fake_driver.classify(RuntimeError("чужое")) is None
    await fake_driver.close_browser(browser)


async def test_disconnect_is_also_reported_on_normal_close(fake_driver: FakeDriver) -> None:
    # Настоящие SDK шлют disconnected и при штатном закрытии: пул должен это переживать.
    browser = await fake_driver.launch(LaunchSpec())
    notified = asyncio.Event()
    fake_driver.on_disconnect(browser, notified.set)

    await fake_driver.close_browser(browser)

    await asyncio.wait_for(notified.wait(), timeout=1)


# --- учёт утечек -----------------------------------------------------------------------

LEAKY_TEST = """
from browser_pool.driver import LaunchSpec

async def test_forgets_to_close(fake_driver):
    await fake_driver.launch(LaunchSpec())
"""

TASK_LEAK_TEST = """
import asyncio

async def test_forgets_a_task(fake_driver):
    fake_driver.keep = asyncio.create_task(asyncio.sleep(100))
"""


@pytest.mark.parametrize("source", [LEAKY_TEST, TASK_LEAK_TEST], ids=["resource", "task"])
def test_leaks_fail_the_test(pytester: pytest.Pytester, source: str) -> None:
    pytester.makeini(
        "[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n"
    )
    pytester.makeconftest('pytest_plugins = ["browser_pool.testing.pytest_plugin"]\n')
    pytester.makepyfile(source)

    result = pytester.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1, errors=1)


# --- возможности -----------------------------------------------------------------------

PROXY = Proxy(host="10.0.0.1", port=8080)


async def test_fake_holds_the_driver_to_its_capabilities(fake_driver: FakeDriver) -> None:
    with pytest.raises(FakeDriverError, match="proxy_scope"):
        await fake_driver.launch(LaunchSpec(proxy=PROXY))

    single = FakeDriver(capabilities=DriverCapabilities(proxy_scope="browser"))
    browser = await single.launch(LaunchSpec(proxy=PROXY))
    with pytest.raises(FakeDriverError, match="proxy_scope"):
        await single.new_context(browser, ContextSpec(proxy=PROXY))
    await single.new_context(browser, ContextSpec())
    with pytest.raises(FakeDriverError, match="can_new_context"):
        await single.new_context(browser, ContextSpec())
    await single.close_browser(browser)


# --- как настоящий браузер ---------------------------------------------------------------


async def test_profile_keeps_only_cookies_with_an_expiry(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    spec = LaunchSpec(user_data_dir=tmp_path / "profile")
    lasting = Cookie(
        name="sid", value="ada", domain="mail.example", expires=utc_now() + timedelta(days=1)
    )
    session = Cookie(name="tmp", value="1", domain="mail.example")
    browser = await fake_driver.launch(spec)
    context = await fake_driver.new_context(browser, ContextSpec(reuse_default=True))
    await fake_driver.add_cookies(context, [lasting, session])
    await fake_driver.close_browser(browser)

    browser = await fake_driver.launch(spec)
    context = await fake_driver.new_context(browser, ContextSpec(reuse_default=True))
    state = await fake_driver.export_state(context)
    await fake_driver.close_browser(browser)

    assert [cookie.name for cookie in state.cookies] == ["sid"]  # сессионная умерла с браузером


async def test_label_page_is_journaled_and_can_be_failed(fake_driver: FakeDriver) -> None:
    browser = await fake_driver.launch(LaunchSpec())
    page = await fake_driver.new_page(await fake_driver.new_context(browser, ContextSpec()))
    fake_driver.faults.fail("label_page", FakeDriverError("окно не подписалось"))

    with pytest.raises(FakeDriverError):
        await fake_driver.label_page(page, "[mail:a] ")
    await fake_driver.label_page(page, "[mail:a] ")

    assert [call.operation for call in fake_driver.calls].count("label_page") == 2
    assert page.label == "[mail:a] "
    await fake_driver.close_browser(browser)


def test_contract_site_never_listens_on_a_port_browsers_refuse() -> None:
    assert not safe_port(6667)
    assert safe_port(54321)
    for _ in range(20):
        with ContractSite() as site:
            assert safe_port(site.port)


THIRD_PARTY_SUITE = """
from browser_pool.testing import FakeDriver
from browser_pool.testing.contract import DriverContractSuite


class TestMyDriver(DriverContractSuite):
    has_process = False

    def make_driver(self):
        self.fake = FakeDriver()
        return self.fake

    async def visit(self, page, url):
        return await self.fake.fetch(page, url)
"""


def test_contract_suite_runs_in_a_project_with_default_pytest_asyncio(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONUTF8", "1")  # вывод дочернего pytest — в UTF-8: в нём русские причины
    # Чужой проект: ни нашего плагина, ни asyncio_mode = "auto" — только pytest-asyncio как есть.
    pytester.makeini(
        "[pytest]"
    )  # корень проекта — каталог теста, а не чужой pyproject выше по дереву
    pytester.makepyfile(test_my_driver=THIRD_PARTY_SUITE)

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-W", "error")

    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0, result.stdout.str()
    assert outcomes.get("errors", 0) == 0, result.stdout.str()
    assert outcomes.get("passed", 0) >= 15
