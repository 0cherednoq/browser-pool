"""`PlaywrightDriver` на настоящем Chromium: аренда, изоляция, состояние, сбои, классификация."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from browser_pool import BrowserPool, ErrorKind, Identity, PoolConfig
from browser_pool.config import Limits, Recycling, Topology
from browser_pool.driver import ContextSpec, LaunchSpec
from browser_pool.evidence import image_suffix
from browser_pool.geometry import Geolocation
from browser_pool.proxies import Proxy, ProxyList
from browser_pool.state import Cookie, MemoryStateStore, SessionState
from browser_pool.testing.contract_site import ContractSite

pytest.importorskip("playwright.async_api")

from browser_pool.drivers.playwright import PlaywrightDriver

pytestmark = pytest.mark.browser

A = Identity(key="mail:a")
B = Identity(key="mail:b")
SID = Cookie(name="sid", value="logged-in", domain="mail.example", path="/")
CLOSED_PORT = 9  # discard: на локальной машине никто не слушает


@pytest_asyncio.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    driver = PlaywrightDriver()
    await driver.prepare()
    try:
        yield driver
    finally:
        await driver.shutdown()


def config() -> PoolConfig:
    return PoolConfig(
        topology=Topology(browsers=1, pages_per_browser=4),
        limits=Limits(spawn_delay=0.0),
        recycling=Recycling(browser_max_leases=None, browser_max_age=None),
    )


async def test_lease_runs_on_a_real_page() -> None:
    async with BrowserPool(PlaywrightDriver(), config=config()) as pool, pool.page(A) as lease:
        await lease.page.set_content("<h1>привет</h1>")

        assert await lease.page.inner_text("h1") == "привет"
        assert lease.browser.is_connected()


async def test_cookies_are_isolated_between_identities() -> None:
    driver = PlaywrightDriver()

    async with BrowserPool(driver, config=config()) as pool:
        async with pool.page(A) as lease:
            await driver.add_cookies(lease.context, [SID])
            assert SID.name in {cookie.name for cookie in await lease.cookies()}
        async with pool.page(B) as lease:
            assert await lease.cookies() == ()


async def test_state_survives_a_new_pool() -> None:
    store = MemoryStateStore()
    driver = PlaywrightDriver()

    async with (
        BrowserPool(driver, config=config(), state_store=store) as pool,
        pool.page(A) as lease,
    ):
        await driver.add_cookies(lease.context, [SID])

    async with (
        BrowserPool(driver, config=config(), state_store=store) as pool,
        pool.page(A) as lease,
    ):
        restored = await lease.cookies(domain="mail.example")

    assert [(cookie.name, cookie.value) for cookie in restored] == [("sid", "logged-in")]


async def test_state_round_trip_keeps_local_storage_and_extras(driver: PlaywrightDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        state = SessionState(cookies=(SID,), extras={"token": "t-1"})
        context = await driver.new_context(browser, ContextSpec(state=state))
        exported = await driver.export_state(context)
        await driver.close_context(context)
    finally:
        await driver.close_browser(browser)

    assert [cookie.name for cookie in exported.cookies] == ["sid"]
    assert exported.extras == {"token": "t-1"}


async def test_context_goes_through_its_proxy(driver: PlaywrightDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    dead = Proxy(host="127.0.0.1", port=CLOSED_PORT)
    try:
        context = await driver.new_context(browser, ContextSpec(proxy=dead))
        page = await driver.new_page(context)
        with pytest.raises(Exception) as caught:  # noqa: PT011 — проверяется классификация, а не тип
            await page.goto("http://example.com", timeout=15_000)
    finally:
        await driver.close_browser(browser)

    assert driver.classify(caught.value) is ErrorKind.proxy


async def test_unreachable_site_is_a_page_fault(driver: PlaywrightDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        context = await driver.new_context(browser, ContextSpec())
        page = await driver.new_page(context)
        with pytest.raises(Exception) as caught:  # noqa: PT011
            await page.goto(f"http://127.0.0.1:{CLOSED_PORT}", timeout=15_000)
    finally:
        await driver.close_browser(browser)

    assert driver.classify(caught.value) is ErrorKind.page
    assert driver.classify(ValueError("чужое")) is None


async def test_killed_browser_stops_answering(driver: PlaywrightDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    disconnected: list[bool] = []
    driver.on_disconnect(browser, lambda: disconnected.append(True))
    assert driver.pid(browser) is not None
    assert await driver.ping(browser)

    await driver.kill_browser(browser)
    for _ in range(50):
        if disconnected:
            break
        await asyncio.sleep(0.1)

    assert disconnected
    assert not await driver.ping(browser)


async def test_pool_with_proxy_list_hands_the_proxy_to_the_lease() -> None:
    proxy = Proxy(host="127.0.0.1", port=CLOSED_PORT, id="dead")

    async with (
        BrowserPool(PlaywrightDriver(), config=config(), proxy_source=ProxyList([proxy])) as pool,
        pool.page(A) as lease,
    ):
        assert lease.proxy == proxy
        await lease.page.set_content("<p>контекст открыт, сеть не нужна</p>")


async def test_window_label_survives_the_site_changing_its_title(driver: PlaywrightDriver) -> None:
    browser = await driver.launch(LaunchSpec(slow_mo=0.0, keep_background_active=True))
    try:
        page = await driver.new_page(await driver.new_context(browser, ContextSpec()))
        await page.set_content("<title>Входящие</title>")
        await driver.label_page(page, "[mail:a] ")
        assert await page.title() == "[mail:a] Входящие"

        await page.evaluate("() => { document.title = 'Новое письмо'; }")
        await asyncio.sleep(0.1)  # MutationObserver срабатывает после задачи
        assert await page.title() == "[mail:a] Новое письмо"
    finally:
        await driver.close_browser(browser)


async def test_simultaneous_windowed_tabs_open_cleanly(driver: PlaywrightDriver) -> None:
    """`window_per_page`: вкладки, открытые разом, — рабочие и каждая в своём окне."""
    browser = await driver.launch(LaunchSpec())
    try:
        context = await driver.new_context(browser, ContextSpec(window_per_page=True))
        pages = await asyncio.gather(*(driver.new_page(context) for _ in range(3)))
        windows = {await driver.window_of(page) for page in pages}
        await asyncio.gather(*(page.set_content("<p>ok</p>") for page in pages))

        assert len(windows) == 3
        assert all(driver.page_usable(page) for page in pages)
    finally:
        await driver.close_browser(browser)


async def test_capture_takes_screenshot_html_and_address(driver: PlaywrightDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        page = await driver.new_page(await driver.new_context(browser, ContextSpec()))
        await page.set_content("<h1>улика</h1>")
        evidence = await driver.capture(page)
    finally:
        await driver.close_browser(browser)

    assert evidence.url is not None
    assert evidence.html is not None
    assert "улика" in evidence.html
    assert evidence.screenshot is not None
    assert image_suffix(evidence.screenshot) in {".png", ".jpg"}


async def test_capture_keeps_what_it_could_take(
    driver: PlaywrightDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        page = await driver.new_page(await driver.new_context(browser, ContextSpec()))
        await page.set_content("<h1>улика</h1>")

        async def broken(**options: object) -> bytes:
            _ = options
            msg = "снимок не снялся"
            raise RuntimeError(msg)

        monkeypatch.setattr(page, "screenshot", broken)
        evidence = await driver.capture(page)
    finally:
        await driver.close_browser(browser)

    assert evidence.screenshot is None
    assert evidence.html is not None  # упавший снимок не лишил разметки
    assert "улика" in evidence.html
    assert evidence.url is not None


async def test_geolocation_adds_to_the_permissions_of_the_driver() -> None:
    driver = PlaywrightDriver(context_options={"permissions": ["notifications"]})
    await driver.prepare()
    try:
        browser = await driver.launch(LaunchSpec())
        try:
            spec = ContextSpec(geolocation=Geolocation(latitude=55.75, longitude=37.61))
            page = await driver.new_page(await driver.new_context(browser, spec))
            with ContractSite() as site:
                await page.goto(site.url("/page"))
                states = [
                    await page.evaluate(
                        "name => navigator.permissions.query({name}).then(result => result.state)",
                        name,
                    )
                    for name in ("notifications", "geolocation")
                ]
        finally:
            await driver.close_browser(browser)
    finally:
        await driver.shutdown()

    assert states == ["granted", "granted"]
