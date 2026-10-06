"""Контрактные тесты драйвера: один набор — для любого адаптера SDK.

Драйвер подключается подклассом с именем `Test…`::

    import pytest
    from browser_pool.testing.contract import DriverContractSuite

    pytestmark = pytest.mark.browser  # настоящему браузеру — обычный цикл событий


    class TestPlaywright(DriverContractSuite[Browser, BrowserContext, Page]):
        def make_driver(self):
            return PlaywrightDriver()

        async def visit(self, page, url):
            await page.goto(url)
            return await page.inner_text("body")

Набор проверяет то, на что опирается пул: контексты не делят куки; состояние сессии — куки со
всеми атрибутами, localStorage, `extras` — переносится в новый контекст, и странная кука сайта его не
ломает; профиль на диске хранит вход между запусками; прокси контекста действительно используется
— с авторизацией, на всех вкладках и с любым паролем, если драйвер её объявил; мёртвый прокси
классифицируется как сбой прокси, а умерший браузер — как сбой браузера; `ping` видит добитый
браузер; закрытие не оставляет процесса; вкладка закрытого контекста непригодна и закрывается без
исключений; локаль, часовой пояс, user agent и viewport контекста применяются; окна встают куда
сказано. Что драйвер не объявил в возможностях — не проверяется.

Нужен pytest-asyncio; настройка `asyncio_mode` не важна — тесты набора помечены сами. Тест, которому
нужен JS на странице, пропускается, пока подкласс не задал `evaluate`.

Необязательное, чего драйвер на `BaseDriver` не переопределил (`capture`, `on_disconnect`, `pid`),
набор не требует; объявленное в возможностях — требует, и первым делом сверяет возможности с
реализацией (`driver_problems`).

Публичный API: `DriverContractSuite` и его переопределяемые методы и атрибуты (`make_driver`,
`visit`, `evaluate`, `crash`, `launch_spec`, `process_alive`, `has_process`). Набор зовёт драйвер из
цикла событий теста, поэтому синхронные драйверы (`thread_affinity`) им пока не проверяются, и
запускает браузер сам (`launch`): подключение (`attach`) он не проверяет.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio

from browser_pool.clock import monotonic, utc_now
from browser_pool.driver import (
    ContextSpec,
    DriverCapabilities,
    LaunchSpec,
    WindowBounds,
    WindowControl,
    WindowState,
    driver_problems,
    uses_default,
)
from browser_pool.errors import ErrorKind
from browser_pool.geometry import Rect, Viewport
from browser_pool.procguard import kill_tree, process_token
from browser_pool.proxies import Proxy
from browser_pool.state import Cookie, Origin, SessionState
from browser_pool.testing.contract_site import FAKE_HOST, ContractProxy, ContractSite, safe_port

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
    from pathlib import Path

    from browser_pool.driver import Driver

_WAIT = 10.0
_STEP = 0.05


_ODD_PASSWORD = "p@ss:w/rd %41"  # noqa: S105 — нарочный пароль: всё, что ломает URL и Basic
_AGENT = "ContractAgent/1.0"


@pytest.mark.asyncio
class DriverContractSuite[B, C, P](ABC):
    """Набор контрактных тестов. Подкласс задаёт `make_driver` и `visit`."""

    has_process: bool = True
    """У запущенного браузера есть процесс ОС (`driver.pid`). `False` — у драйверов без процессов."""

    @abstractmethod
    def make_driver(self) -> Driver[B, C, P]:
        """Новый драйвер для теста."""

    @abstractmethod
    async def visit(self, page: P, url: str) -> str:
        """Открыть адрес во вкладке и вернуть текст страницы — средствами SDK."""

    async def evaluate(self, page: P, expression: str) -> object:
        """Вычислить JS-выражение во вкладке и вернуть значение — средствами SDK.

        По умолчанию не умеет: тесты, которым нужен JS (localStorage, эмуляция), пропускаются.
        """
        _ = page, expression
        pytest.skip("подкласс набора не задал evaluate — тесту нужен JS на странице")

    async def crash(self, driver: Driver[B, C, P], browser: B) -> None:
        """Уронить браузер так, как он падает сам, — не через драйвер. По умолчанию — убить процесс."""
        pid = driver.pid(browser)
        if pid is None:
            pytest.skip("драйвер не знает процесс браузера — уронить его нечем")
        await asyncio.to_thread(kill_tree, pid)

    def launch_spec(self) -> LaunchSpec:
        """С чем запускать браузер в тестах набора."""
        return LaunchSpec()

    def process_alive(self, driver: Driver[B, C, P], pid: int) -> bool:
        """Жив ли процесс браузера. Драйвер без настоящих процессов переопределяет."""
        _ = driver
        return process_token(pid) is not None

    # --- фикстуры ----------------------------------------------------------------------

    @pytest_asyncio.fixture
    async def driver(self) -> AsyncIterator[Driver[B, C, P]]:
        """Подготовленный драйвер; после теста — `shutdown`."""
        driver = self.make_driver()
        await driver.prepare()
        try:
            yield driver
        finally:
            await driver.shutdown()

    @pytest.fixture
    def site(self) -> Iterator[ContractSite]:
        """Локальный сайт с логином."""
        with ContractSite() as site:
            yield site

    # --- возможности -------------------------------------------------------------------

    async def test_capabilities_match_the_driver(self, driver: Driver[B, C, P]) -> None:
        """Объявленное в возможностях реализовано: иначе пул такой драйвер не примет."""
        assert driver_problems(driver) == ()

    # --- изоляция и состояние ----------------------------------------------------------

    async def test_contexts_do_not_share_cookies(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Вход в одном контексте не виден в другом."""
        self._require(driver.capabilities.can_new_context, "драйвер не создаёт контексты")
        async with self._browser(driver) as browser:
            first = await driver.new_page(await driver.new_context(browser, ContextSpec()))
            second = await driver.new_page(await driver.new_context(browser, ContextSpec()))

            assert await self.visit(first, site.url("/login?user=ada")) == "ok"
            assert await self.visit(first, site.url("/whoami")) == "ada"
            assert await self.visit(second, site.url("/whoami")) == "anonymous"

    async def test_state_moves_to_a_new_context(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Снятое состояние, заданное новому контексту, делает вход «уже бывшим»."""
        self._require(driver.capabilities.state_support != "none", "драйвер без состояния")
        async with self._browser(driver) as browser:
            context = await driver.new_context(browser, ContextSpec())
            await self.visit(await driver.new_page(context), site.url("/login?user=ada"))
            state = await driver.export_state(context)
            await driver.close_context(context)

            assert "sid" in {cookie.name for cookie in state.cookies}
            restored = await driver.new_context(browser, ContextSpec(state=state))
            assert await self.visit(await driver.new_page(restored), site.url("/whoami")) == "ada"

    async def test_cookie_attributes_survive_export(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Кука настоящего сайта — со сроком, `HttpOnly`, `SameSite` — снимается со всеми атрибутами."""
        self._require(driver.capabilities.state_support != "none", "драйвер без состояния")
        async with self._browser(driver) as browser:
            context = await driver.new_context(browser, ContextSpec())
            await self.visit(await driver.new_page(context), site.url("/login?user=ada&rich=1"))

            state = await driver.export_state(context)

            (sid,) = [cookie for cookie in state.cookies if cookie.name == "sid"]
            assert sid.http_only
            assert sid.same_site == "Lax"
            assert sid.expires is not None
            assert sid.expires > utc_now()

    async def test_odd_cookie_does_not_break_export(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Кука, которую модель не представит (без имени), не мешает снять остальные."""
        self._require(driver.capabilities.state_support != "none", "драйвер без состояния")
        async with self._browser(driver) as browser:
            context = await driver.new_context(browser, ContextSpec())
            page = await driver.new_page(context)
            await self.visit(page, site.url("/login?user=ada"))
            await self.visit(page, site.url("/odd-cookie"))

            state = await driver.export_state(context)

            assert "sid" in {cookie.name for cookie in state.cookies}

    async def test_local_storage_moves_to_a_new_context(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Драйвер с `state_support="full"`: localStorage снимается и восстанавливается в свой origin."""
        self._require(
            driver.capabilities.state_support == "full", "драйвер не переносит localStorage"
        )
        async with self._browser(driver) as browser:
            context = await driver.new_context(browser, ContextSpec())
            page = await driver.new_page(context)
            await self.visit(page, site.url("/page"))
            await self.evaluate(page, "localStorage.setItem('token', 'abc')")
            state = await driver.export_state(context)
            await driver.close_context(context)

            assert Origin(origin=site.url(""), local_storage=(("token", "abc"),)) in state.origins
            restored = await driver.new_context(browser, ContextSpec(state=state))
            page = await driver.new_page(restored)
            await self.visit(page, site.url("/page"))
            assert await self.evaluate(page, "localStorage.getItem('token')") == "abc"

    async def test_extras_come_back_with_the_state(self, driver: Driver[B, C, P]) -> None:
        """`extras` состояния пул хранит для site SDK: драйвер возвращает их при снятии как получил."""
        self._require(driver.capabilities.state_support != "none", "драйвер без состояния")
        given = SessionState(extras={"token": "abc", "headers": {"x-app": "1"}})
        async with self._browser(driver) as browser:
            context = await driver.new_context(browser, ContextSpec(state=given))

            state = await driver.export_state(context)

            assert dict(state.extras) == {"token": "abc", "headers": {"x-app": "1"}}

    async def test_added_cookies_reach_the_site(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """`add_cookies` в живой контекст — сайт их видит."""
        self._require(not uses_default(driver, "add_cookies"), "драйвер не добавляет куки")
        async with self._browser(driver) as browser:
            context = await driver.new_context(browser, ContextSpec())
            await driver.add_cookies(context, [Cookie(name="sid", value="bob", domain="127.0.0.1")])

            assert await self.visit(await driver.new_page(context), site.url("/whoami")) == "bob"

    async def test_profile_keeps_login_across_launches(
        self, driver: Driver[B, C, P], site: ContractSite, tmp_path: Path
    ) -> None:
        """Драйвер с `persistent_dir`: вход в готовом контексте профиля переживает перезапуск."""
        self._require(driver.capabilities.persistent_dir, "драйвер без профиля на диске")
        spec = self.launch_spec()
        spec.user_data_dir = tmp_path / "profile"
        # На диск браузеры пишут только куки со сроком: сессионные живут до закрытия.
        lasting = Cookie(
            name="sid", value="bob", domain="127.0.0.1", expires=utc_now() + timedelta(days=30)
        )
        browser = await driver.launch(spec)
        try:
            await driver.add_cookies(
                await driver.new_context(browser, ContextSpec(reuse_default=True)), [lasting]
            )
        finally:
            await driver.close_browser(browser)

        browser = await driver.launch(spec)
        try:
            context = await driver.new_context(browser, ContextSpec(reuse_default=True))
            assert await self.visit(await driver.new_page(context), site.url("/whoami")) == "bob"
            # Готовый контекст — не пула: «закрытие» оставляет его рабочим.
            await driver.close_context(context)
            again = await driver.new_context(browser, ContextSpec(reuse_default=True))
            assert await self.visit(await driver.new_page(again), site.url("/whoami")) == "bob"
        finally:
            await driver.close_browser(browser)

    # --- прокси ------------------------------------------------------------------------

    async def test_proxy_of_the_context_is_used(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Хост, который резолвит только прокси, открывается — значит, запрос шёл через него."""
        with ContractProxy(site) as proxy:
            page = await self._page_behind(driver, Proxy(host="127.0.0.1", port=proxy.port))
            try:
                assert await self.visit(page.page, site.url("/whoami", host=FAKE_HOST)) == (
                    "anonymous"
                )
            finally:
                await page.close()

            assert FAKE_HOST in proxy.hosts

    async def test_proxy_credentials_are_sent(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Драйвер с `proxy_auth` проходит прокси с логином и паролем."""
        self._require(driver.capabilities.proxy_auth, "драйвер не умеет авторизацию на прокси")
        with ContractProxy(site, username="ada", password="pa55") as proxy:
            secured = Proxy(host="127.0.0.1", port=proxy.port, username="ada", password="pa55")
            page = await self._page_behind(driver, secured)
            try:
                assert await self.visit(page.page, site.url("/whoami", host=FAKE_HOST)) == (
                    "anonymous"
                )
            finally:
                await page.close()

            assert FAKE_HOST in proxy.hosts

    async def test_every_page_gets_through_the_proxy_with_credentials(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Прокси с логином: вторая вкладка контекста и вторая навигация проходят так же, как первые."""
        self._require(driver.capabilities.proxy_auth, "драйвер не умеет авторизацию на прокси")
        self._require(driver.capabilities.proxy_scope == "context", "прокси не на контексте")
        with ContractProxy(site, username="ada", password="pa55") as proxy:
            secured = Proxy(host="127.0.0.1", port=proxy.port, username="ada", password="pa55")
            async with self._browser(driver) as browser:
                context = await driver.new_context(browser, ContextSpec(proxy=secured))
                first, second = await driver.new_page(context), await driver.new_page(context)
                for page in (first, second, first, second):
                    assert await self.visit(page, site.url("/whoami", host=FAKE_HOST)) == (
                        "anonymous"
                    )

            assert proxy.hosts.count(FAKE_HOST) >= 4  # noqa: PLR2004 — четыре навигации

    async def test_proxy_password_with_special_characters(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Пароль прокси с `@`, `:`, `/`, `%` и пробелом доходит до прокси как есть."""
        self._require(driver.capabilities.proxy_auth, "драйвер не умеет авторизацию на прокси")
        with ContractProxy(site, username="ada", password=_ODD_PASSWORD) as proxy:
            secured = Proxy(
                host="127.0.0.1", port=proxy.port, username="ada", password=_ODD_PASSWORD
            )
            page = await self._page_behind(driver, secured)
            try:
                assert await self.visit(page.page, site.url("/whoami", host=FAKE_HOST)) == (
                    "anonymous"
                )
            finally:
                await page.close()

    async def test_rejected_proxy_credentials_are_a_proxy_fault(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Прокси не принял креды — `classify` говорит `proxy`, и навигация не висит в повторах."""
        self._require(driver.capabilities.proxy_auth, "драйвер не умеет авторизацию на прокси")
        with ContractProxy(site, username="ada", password="pa55") as proxy:
            wrong = Proxy(host="127.0.0.1", port=proxy.port, username="ada", password="other")
            page = await self._page_behind(driver, wrong)
            try:
                with pytest.raises(Exception) as caught:  # noqa: PT011 — проверяется классификация
                    await self.visit(page.page, site.url("/whoami", host=FAKE_HOST))
            finally:
                await page.close()

        assert driver.classify(caught.value) is ErrorKind.proxy
        assert FAKE_HOST not in proxy.hosts

    async def test_dead_proxy_is_a_proxy_fault(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Прокси не отвечает — `classify` говорит `proxy`: пул сменит прокси, а не identity."""
        page = await self._page_behind(driver, Proxy(host="127.0.0.1", port=_free_port()))
        try:
            with pytest.raises(Exception) as caught:  # noqa: PT011 — проверяется классификация
                await self.visit(page.page, site.url("/whoami", host=FAKE_HOST))
        finally:
            await page.close()

        assert driver.classify(caught.value) is ErrorKind.proxy

    async def test_foreign_errors_are_not_classified(self, driver: Driver[B, C, P]) -> None:
        """Чужую ошибку драйвер не присваивает."""
        assert driver.classify(ValueError("не из SDK")) is None
        assert isinstance(driver.capabilities, DriverCapabilities)

    # --- жизнь браузера ----------------------------------------------------------------

    async def test_killed_browser_is_noticed(self, driver: Driver[B, C, P]) -> None:
        """После `kill_browser` — `ping` → `False` и, если драйвер сообщает об обрыве, событие."""
        browser = await driver.launch(self.launch_spec())
        gone: list[bool] = []
        driver.on_disconnect(browser, lambda: gone.append(True))
        assert await driver.ping(browser)

        await driver.kill_browser(browser)

        if not uses_default(driver, "on_disconnect"):
            assert await _eventually(lambda: bool(gone)), "нет события обрыва после kill_browser"
        assert not await _ping_quietly(driver, browser)
        with contextlib.suppress(Exception):
            await driver.close_browser(browser)

    async def test_dead_browser_is_a_browser_fault(self, driver: Driver[B, C, P]) -> None:
        """Браузер умер сам: о нём узнают событием и `ping`, а ошибка его вкладок — вида `browser`.

        По виду пул отправляет в карантин браузер, а не винит identity и не выбрасывает одну вкладку.
        """
        browser = await driver.launch(self.launch_spec())
        try:
            context = await driver.new_context(browser, ContextSpec())
            await driver.new_page(context)
            gone: list[bool] = []
            driver.on_disconnect(browser, lambda: gone.append(True))

            await self.crash(driver, browser)

            if not uses_default(driver, "on_disconnect"):
                assert await _eventually(lambda: bool(gone)), "нет события обрыва после падения"
            assert not await _ping_quietly(driver, browser)
            with pytest.raises(Exception) as caught:  # noqa: PT011 — проверяется классификация
                async with asyncio.timeout(_WAIT):
                    await driver.new_page(context)
            assert driver.classify(caught.value) is ErrorKind.browser
        finally:
            with contextlib.suppress(Exception):
                await driver.kill_browser(browser)

    async def test_closed_browser_leaves_no_process(self, driver: Driver[B, C, P]) -> None:
        """После `close_browser` процесса браузера нет."""
        browser = await driver.launch(self.launch_spec())
        pid = driver.pid(browser)
        if pid is None:
            await driver.close_browser(browser)
            assert not self.has_process or uses_default(driver, "pid"), (
                "driver.pid() не знает процесс запущенного браузера: пулу нечем добить зависший. "
                "У драйвера без процессов задайте has_process = False"
            )
            pytest.skip("у драйвера нет процессов браузера")
        assert self.process_alive(driver, pid)

        await driver.close_browser(browser)

        assert await _eventually(lambda: not self.process_alive(driver, pid)), (
            f"процесс {pid} жив после close_browser"
        )

    async def test_capture_takes_the_open_page(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Улики с живой вкладки: хотя бы адрес; разметка, если снята, — та самая страница."""
        self._require(not uses_default(driver, "capture"), "драйвер не снимает улики")
        async with self._browser(driver) as browser:
            page = await driver.new_page(await driver.new_context(browser, ContextSpec()))
            await self.visit(page, site.url("/whoami"))

            evidence = await driver.capture(page)

            assert evidence.url is not None, "capture не снял даже адрес вкладки"
            assert evidence.url.endswith("/whoami")
            if evidence.html is not None:
                assert "anonymous" in evidence.html

    async def test_closed_page_is_not_usable(self, driver: Driver[B, C, P]) -> None:
        """Закрытая вкладка — непригодна; снимок с неё не бросает."""
        async with self._browser(driver) as browser:
            page = await driver.new_page(await driver.new_context(browser, ContextSpec()))
            assert driver.page_usable(page)

            await driver.close_page(page)

            assert not driver.page_usable(page)
            await driver.capture(page)

    async def test_page_of_a_closed_context_is_dead_and_closes_quietly(
        self, driver: Driver[B, C, P]
    ) -> None:
        """Вкладка умерла не от `close_page`: она непригодна, а закрытие её и контекста не бросает."""
        self._require(driver.capabilities.can_new_context, "драйвер не создаёт контексты")
        async with self._browser(driver) as browser:
            context = await driver.new_context(browser, ContextSpec())
            page = await driver.new_page(context)

            await driver.close_context(context)

            assert not driver.page_usable(page)
            await driver.close_page(page)
            await driver.close_context(context)

    # --- эмуляция ----------------------------------------------------------------------

    async def test_context_settings_are_applied(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Объявленные настройки контекста (`context_settings`) из `ContextSpec` видны странице."""
        applies = driver.capabilities.context_settings
        self._require(driver.capabilities.can_new_context, "драйвер не создаёт контексты")
        self._require(bool(applies), "драйвер не объявил настроек контекста")
        spec = ContextSpec(
            locale="de-DE" if "locale" in applies else None,
            timezone="Asia/Tokyo" if "timezone" in applies else None,
            user_agent=_AGENT if "user_agent" in applies else None,
            viewport=Viewport(width=800, height=600) if "viewport" in applies else None,
        )
        probes = {
            "locale": ("navigator.language", "de-DE"),
            "timezone": ("Intl.DateTimeFormat().resolvedOptions().timeZone", "Asia/Tokyo"),
            "user_agent": ("navigator.userAgent", _AGENT),
            "viewport": ("window.innerWidth", 800),
        }
        async with self._browser(driver) as browser:
            page = await driver.new_page(await driver.new_context(browser, spec))
            await self.visit(page, site.url("/page"))

            for setting, (expression, expected) in probes.items():
                if setting in applies:
                    assert await self.evaluate(page, expression) == expected, setting

    async def test_locale_does_not_strip_client_hints(
        self, driver: Driver[B, C, P], site: ContractSite
    ) -> None:
        """Локаль контекста не обнуляет client hints: пустой `userAgentData.brands` выдаёт автоматизацию."""
        self._require(driver.capabilities.can_new_context, "драйвер не создаёт контексты")
        self._require(
            "locale" in driver.capabilities.context_settings, "драйвер не применяет локаль"
        )
        brands = "navigator.userAgentData ? navigator.userAgentData.brands.length : -1"
        async with self._browser(driver) as browser:
            plain = await driver.new_page(await driver.new_context(browser, ContextSpec()))
            await self.visit(plain, site.url("/page"))
            expected = await self.evaluate(plain, brands)
            tuned = await driver.new_page(
                await driver.new_context(browser, ContextSpec(locale="de-DE"))
            )
            await self.visit(tuned, site.url("/page"))

            assert await self.evaluate(tuned, brands) == expected

    # --- окна --------------------------------------------------------------------------

    async def test_windows_move_where_asked(self, driver: Driver[B, C, P]) -> None:
        """Драйвер с `window_control="runtime"`: окно встаёт куда сказано, сворачивается и возвращается."""
        self._require(
            driver.capabilities.window_control == "runtime" and isinstance(driver, WindowControl),
            "драйвер не двигает окна на лету",
        )
        control = cast("WindowControl[B, P]", driver)
        # Не меньше минимального окна Chrome (~534 px в ширину): окно у́же он молча расширяет.
        target = Rect(x=10, y=20, width=640, height=480)
        async with self._browser(driver) as browser:
            spec = ContextSpec(window_per_page=driver.capabilities.new_window)
            context = await driver.new_context(browser, spec)
            first, second = await driver.new_page(context), await driver.new_page(context)
            window = await control.window_of(first)
            if driver.capabilities.new_window:
                assert await control.window_of(second) != window, "у вкладок должны быть свои окна"
            area = await control.screen_area(first)
            assert area.width > 0
            assert area.height > 0

            await control.set_bounds(browser, window, WindowBounds(rect=target))
            assert await control.get_bounds(browser, window) == WindowBounds(rect=target)
            await control.set_bounds(
                browser, window, WindowBounds(rect=target, state=WindowState.minimized)
            )
            assert (await control.get_bounds(browser, window)).state is WindowState.minimized
            await control.set_bounds(browser, window, WindowBounds(rect=target))
            assert await control.get_bounds(browser, window) == WindowBounds(rect=target)
            await control.bring_to_front(first)

    # --- внутреннее --------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _browser(self, driver: Driver[B, C, P]) -> AsyncGenerator[B]:
        browser = await driver.launch(self.launch_spec())
        try:
            yield browser
        finally:
            await driver.close_browser(browser)

    async def _page_behind(self, driver: Driver[B, C, P], proxy: Proxy) -> _ProxiedPage[B, C, P]:
        """Вкладка за прокси — на контексте или на браузере, как драйвер умеет."""
        scope = driver.capabilities.proxy_scope
        self._require(scope != "external", "прокси задаёт вендор браузера")
        spec = self.launch_spec()
        if scope == "browser":
            spec.proxy = proxy
        browser = await driver.launch(spec)
        try:
            context_spec = ContextSpec(proxy=proxy) if scope == "context" else ContextSpec()
            page = await driver.new_page(await driver.new_context(browser, context_spec))
        except BaseException:
            await driver.close_browser(browser)
            raise
        return _ProxiedPage(driver, browser, page)

    @staticmethod
    def _require(condition: bool, reason: str) -> None:  # noqa: FBT001 — условие, а не флаг
        if not condition:
            pytest.skip(reason)


class _ProxiedPage[B, C, P]:
    def __init__(self, driver: Driver[B, C, P], browser: B, page: P) -> None:
        self._driver = driver
        self._browser = browser
        self.page = page

    async def close(self) -> None:
        await self._driver.close_browser(self._browser)


async def _eventually(check: Callable[[], bool]) -> bool:
    deadline = monotonic() + _WAIT
    while monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(_STEP)
    return bool(check())


async def _ping_quietly[B, C, P](driver: Driver[B, C, P], browser: B) -> bool:
    try:
        async with asyncio.timeout(_WAIT):
            return await driver.ping(browser)
    except Exception:  # noqa: BLE001 — упавший ping и есть «не отвечает»
        return False


def _free_port() -> int:
    """Порт, который никто не слушает: заняли и сразу отпустили."""
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port: int = probe.getsockname()[1]
        if safe_port(port):  # на небезопасный порт браузер не пойдёт вовсе — это не сбой прокси
            return port


__all__ = ["DriverContractSuite"]
