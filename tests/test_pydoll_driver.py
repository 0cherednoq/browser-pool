"""`PydollDriver` на системном Chrome: аренда, изоляция, состояние, эмуляция, подписи окон."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio

from browser_pool import BrowserPool, ContextOptions, Identity, PoolConfig
from browser_pool.config import Limits, Recycling, Topology
from browser_pool.driver import ContextSpec, LaunchSpec
from browser_pool.errors import ErrorKind
from browser_pool.evidence import image_suffix
from browser_pool.geometry import Viewport
from browser_pool.identity import StatePolicy
from browser_pool.proxies import Proxy
from browser_pool.state import Cookie, MemoryStateStore, SessionState
from browser_pool.testing.contract_site import FAKE_HOST, ContractProxy, ContractSite

pytest.importorskip("pydoll")

from browser_pool.drivers import pydoll as pydoll_driver
from browser_pool.drivers.pydoll import PydollDriver

if TYPE_CHECKING:
    from pydoll.browser.tab import Tab

pytestmark = [
    pytest.mark.browser,
    pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction':DeprecationWarning"),
]

A = Identity(key="mail:a")
B = Identity(key="mail:b")
SID = Cookie(name="sid", value="logged-in", domain="mail.example", path="/")
CLOSED_PORT = 9  # discard: на локальной машине никто не слушает


@pytest_asyncio.fixture
async def driver() -> AsyncIterator[PydollDriver]:
    driver = PydollDriver()
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


async def evaluate(tab: Tab, script: str) -> Any:
    response = cast("dict[str, Any]", await tab.execute_script(script, return_by_value=True))
    return response["result"]["result"].get("value")


async def test_lease_runs_on_a_real_tab() -> None:
    async with BrowserPool(PydollDriver(), config=config()) as pool, pool.page(A) as lease:
        await evaluate(lease.page, "document.body.innerHTML = '<h1>привет</h1>'")

        assert await evaluate(lease.page, "document.querySelector('h1').innerText") == "привет"
        assert lease.context.browser is lease.browser


async def test_cookies_are_isolated_between_identities() -> None:
    driver = PydollDriver()

    async with BrowserPool(driver, config=config()) as pool:
        async with pool.page(A) as lease:
            await driver.add_cookies(lease.context, [SID])
            assert SID.name in {cookie.name for cookie in await lease.cookies()}
        async with pool.page(B) as lease:
            assert await lease.cookies() == ()


async def test_state_survives_a_new_pool() -> None:
    store = MemoryStateStore()
    driver = PydollDriver()

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


async def test_state_keeps_cookies_and_extras(driver: PydollDriver) -> None:
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


async def test_identity_emulation_reaches_every_tab() -> None:
    identity = Identity(
        key="shop:de",
        context_options=ContextOptions(
            locale="de-DE",
            timezone="Asia/Tokyo",
            viewport=Viewport(width=800, height=600),
            user_agent="browser-pool-test/1.0",
        ),
    )
    async with BrowserPool(PydollDriver(), config=config()) as pool, pool.page(identity) as lease:
        tab = lease.page
        zone = await evaluate(tab, "Intl.DateTimeFormat().resolvedOptions().timeZone")
        assert zone == "Asia/Tokyo"
        assert await evaluate(tab, "navigator.language") == "de-DE"
        assert await evaluate(tab, "Intl.DateTimeFormat().resolvedOptions().locale") == "de-DE"
        assert await evaluate(tab, "navigator.userAgent") == "browser-pool-test/1.0"
        assert await evaluate(tab, "[innerWidth, innerHeight]") == [800, 600]


async def test_unreachable_site_is_a_page_fault(driver: PydollDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        tab = await driver.new_page(await driver.new_context(browser, ContextSpec()))
        with pytest.raises(Exception) as caught:  # noqa: PT011 — проверяется классификация
            await tab.go_to(f"http://127.0.0.1:{CLOSED_PORT}", timeout=15)
    finally:
        await driver.close_browser(browser)

    assert driver.classify(caught.value) is ErrorKind.page


async def test_login_survives_in_profile_on_disk(tmp_path: Path) -> None:
    # Без хранилища пула (`mode="none"`): вход переживает перезапуск только через профиль Chrome.
    # На диск Chrome пишет только куки со сроком — сессионные живут до закрытия браузера.
    profile = tmp_path / "profiles" / "a"
    identity = Identity(key="mail:a", state=StatePolicy(mode="none", user_data_dir=profile))
    lasting = Cookie(
        name="sid",
        value="logged-in",
        domain="mail.example",
        expires=datetime.now(UTC) + timedelta(days=30),
    )
    driver = PydollDriver()

    async with BrowserPool(driver, config=config()) as pool, pool.page(identity) as lease:
        await driver.add_cookies(lease.context, [lasting])
    assert (profile / "Default").is_dir()  # профиль на месте: pydoll его не удалил

    async with BrowserPool(driver, config=config()) as pool, pool.page(identity) as lease:
        restored = await lease.cookies(domain="mail.example")

    assert [(cookie.name, cookie.value) for cookie in restored] == [("sid", "logged-in")]


async def test_window_label_survives_the_site_changing_its_title(driver: PydollDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        tab = await driver.new_page(await driver.new_context(browser, ContextSpec()))
        await evaluate(tab, "document.title = 'Входящие'")
        await driver.label_page(tab, "[mail:a] ")
        assert await evaluate(tab, "document.title") == "[mail:a] Входящие"

        await evaluate(tab, "document.title = 'Новое письмо'")
        await asyncio.sleep(0.1)  # MutationObserver срабатывает после задачи
        assert await evaluate(tab, "document.title") == "[mail:a] Новое письмо"
    finally:
        await driver.close_browser(browser)


async def test_capture_takes_screenshot_html_and_address(driver: PydollDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        tab = await driver.new_page(await driver.new_context(browser, ContextSpec()))
        await evaluate(tab, "document.body.innerHTML = '<h1>улика</h1>'")
        evidence = await driver.capture(tab)
    finally:
        await driver.close_browser(browser)

    assert evidence.url is not None
    assert evidence.html is not None
    assert "улика" in evidence.html
    assert evidence.screenshot is not None
    assert image_suffix(evidence.screenshot) in {".png", ".jpg"}


def _refused_in(module: str) -> ConnectionRefusedError:
    """`ConnectionRefusedError`, поднятая кодом модуля `module`."""
    code = compile("raise ConnectionRefusedError(1225, 'отклонено')", f"<{module}>", "exec")
    try:
        exec(code, {"__name__": module})  # noqa: S102 — кадр с нужным именем модуля
    except ConnectionRefusedError as error:
        return error
    raise AssertionError


def test_dead_browser_connection_is_a_browser_fault(driver: PydollDriver) -> None:
    # Вкладка умершего браузера переподключается — pydoll отдаёт ошибку ОС как есть.
    assert driver.classify(_refused_in("pydoll.connection.connection_handler")) is ErrorKind.browser
    # Та же ошибка из кода приложения — не браузер: пул не должен карантинить здоровый.
    assert driver.classify(_refused_in("my_app.http")) is None
    assert driver.classify(ConnectionRefusedError()) is None


async def test_profile_browser_gets_through_a_proxy_with_credentials(
    driver: PydollDriver, tmp_path: Path
) -> None:
    # Прокси запуска (профиль на диске): браузеру уходит адрес без кредов, отвечает на авторизацию драйвер.
    password = "p@ss:1"
    with ContractSite() as site, ContractProxy(site, username="ada", password=password) as proxy:
        secured = Proxy(host="127.0.0.1", port=proxy.port, username="ada", password=password)
        browser = await driver.launch(LaunchSpec(user_data_dir=tmp_path / "profile", proxy=secured))
        try:
            context = await driver.new_context(browser, ContextSpec(reuse_default=True))
            for _ in range(2):  # и вторая вкладка тоже
                tab = await driver.new_page(context)
                await tab.go_to(site.url("/whoami", host=FAKE_HOST), timeout=15)
        finally:
            await driver.close_browser(browser)

        assert proxy.hosts.count(FAKE_HOST) >= 2


# --- жизненный цикл --------------------------------------------------------------------------

ZONE = "Intl.DateTimeFormat().resolvedOptions().timeZone"


async def test_cancelled_launch_leaves_no_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    spawned = asyncio.Event()
    spawn = pydoll_driver._quiet_process  # pyright: ignore[reportPrivateUsage]

    def recording(command: list[str]) -> subprocess.Popen[bytes]:
        process = spawn(command)
        started.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(pydoll_driver, "_quiet_process", recording)
    driver = PydollDriver()

    # Тайм-аут пула посреди запуска — это отмена. Отменяем, как только процесс появился: фиксированный
    # срок зависел бы от скорости машины (быстрый Chrome успевал стартовать целиком).
    launch = asyncio.ensure_future(driver.launch(LaunchSpec()))
    await asyncio.wait_for(spawned.wait(), timeout=30)
    launch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launch

    assert started, "процесс не успел стартовать — тест ничего не проверил"
    assert all(process.poll() is not None for process in started)


async def test_tab_closed_behind_the_driver_is_known_dead(driver: PydollDriver) -> None:
    from pydoll.commands import TargetCommands

    browser = await driver.launch(LaunchSpec())
    try:
        context = await driver.new_context(browser, ContextSpec())
        tab = await driver.new_page(context)
        target = tab._target_id  # pyright: ignore[reportPrivateUsage]
        assert target is not None
        assert driver.page_usable(tab)

        # Как `window.close()` сайта или закрытие окна человеком: вкладку закрыл не драйвер.
        await browser._execute_command(TargetCommands.close_target(target))  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.5)

        assert not driver.page_usable(tab)
        await driver.close_page(tab)
    finally:
        await driver.close_browser(browser)


async def test_context_new_tab_comes_out_ready(driver: PydollDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        spec = ContextSpec(locale="de-DE", timezone="Asia/Tokyo")
        context = await driver.new_context(browser, spec)

        tab = await context.new_tab()  # так вкладки открывает site SDK

        assert await evaluate(tab, ZONE) == "Asia/Tokyo"
        assert await evaluate(tab, "navigator.language") == "de-DE"
    finally:
        await driver.close_browser(browser)


async def test_tab_opened_around_the_driver_is_emulated_too(driver: PydollDriver) -> None:
    browser = await driver.launch(LaunchSpec())
    try:
        context = await driver.new_context(browser, ContextSpec(timezone="Asia/Tokyo"))

        tab = await browser.new_tab(browser_context_id=context.id)  # в обход драйвера
        await asyncio.sleep(1.0)  # драйвер узнаёт о ней по событию браузера

        assert await evaluate(tab, ZONE) == "Asia/Tokyo"
    finally:
        await driver.close_browser(browser)


async def test_one_pool_leaving_does_not_blind_the_other() -> None:
    driver = PydollDriver()
    await driver.prepare()  # первый пул
    await driver.prepare()  # второй пул на том же драйвере
    browser = await driver.launch(LaunchSpec())
    gone: list[bool] = []
    driver.on_disconnect(browser, lambda: gone.append(True))

    await driver.shutdown()  # первый пул ушёл — сторож браузера второго работает
    await driver.kill_browser(browser)
    for _ in range(40):
        if gone:
            break
        await asyncio.sleep(0.25)

    assert gone
    await driver.shutdown()
