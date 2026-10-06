"""Драйвер pydoll: Chrome и Edge по CDP, без WebDriver.

Много identity в одном браузере — каждая в своём контексте браузера (`browserContextId`) со
своим прокси и куками. Контекста-объекта у pydoll нет, поэтому контекст пула — `PydollContext`:
браузер и номер контекста. Site SDK получает его в аренде и открывает вкладки сам, если нужно::

    tab = await lease.context.new_tab()

Именно так, а не `browser.new_tab(browser_context_id=…)`: вкладка от `context.new_tab()` выходит
готовой — с авторизацией на прокси и эмуляцией identity. Вкладку, открытую в обход (и попап,
который сайт открыл сам через `window.open`), драйвер замечает по событию браузера и эмуляцию
применяет следом — но первые скрипты такой вкладки успевают увидеть настоящие локаль и часовой
пояс хоста, а авторизации на прокси у неё нет (её обычно и не нужно: контекст помнит креды).

    driver = PydollDriver(arguments=["--lang=de-DE"])
    async with BrowserPool(driver, config=config) as pool:
        ...

Что делает драйвер сверх SDK:

- авторизацию на прокси (контекста или запуска) драйвер делает сам: браузеру отдаётся адрес без
  кредов, а на каждой вкладке перехватываются навигации (`Fetch`, только документы) и на запрос
  авторизации прокси отвечают креды как есть. Встроенная авторизация pydoll для этого не годится:
  она вешает на вкладку одноразовые обработчики и не снимает перехват, если запроса авторизации не
  было, — вторая вкладка контекста повисает. Креды, которые прокси не принял, второй раз не
  предлагаются: навигация падает сбоем прокси. Для socks Chromium авторизацию не поддерживает.
  Вкладке, которую site SDK включил в свой перехват `Fetch`, он же и отвечает на авторизацию;
- состояние сессии — только куки: localStorage pydoll не выгружает (`state_support="cookies"`);
- локаль, часовой пояс, геопозиция, размер страницы и user agent identity задаются каждой новой
  вкладке контекста через CDP `Emulation.*`. Локаль в `navigator.language` и `Accept-Language`
  CDP меняет только вместе со строкой user agent, и без client hints такая подмена обнуляет
  `navigator.userAgentData` — заметный признак автоматизации. Поэтому настоящие client hints
  браузера снимаются один раз (со служебной вкладки `chrome://version`) и передаются вместе с
  локалью. Со своим `user_agent` client hints не передаются: настоящие ему противоречили бы;
- вкладку, закрытую не драйвером (сайт, контекст, человек), драйвер узнаёт по событию браузера:
  она непригодна сразу, а не при первой неудачной команде;
- запуск, который отменили или который не удался, не оставляет процесса Chrome;
- процесс браузера запускается с выводом в `DEVNULL`: pydoll открывает ему трубы и не читает
  их — долгоживущий браузер повис бы на заполненной трубе;
- закрытие ждёт выхода процесса в потоке: `Browser.stop()` pydoll ждёт его синхронно, блокируя
  цикл событий до 15 секунд; об обрыве сообщает сторож процесса — событий обрыва у pydoll нет.

Команды CDP, для которых у pydoll нет публичного метода (окна, эмуляция, скрипт при загрузке),
идут через его внутренний `_execute_command` — все в одном месте, `_cdp`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import subprocess
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast, override

from browser_pool._choice import require_choice
from browser_pool.driver import (
    CONTEXT_SETTINGS,
    BaseDriver,
    DriverCapabilities,
    Evidence,
    WindowBounds,
    WindowState,
)
from browser_pool.errors import ErrorKind, UnsupportedRequirementError
from browser_pool.geometry import Rect
from browser_pool.state import Cookie, SessionState

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

    from pydoll.browser import Chrome, Edge
    from pydoll.browser.chromium.base import Browser
    from pydoll.browser.tab import Tab
    from pydoll.protocol.network.types import Cookie as CdpCookie
    from pydoll.protocol.network.types import CookieParam

    from browser_pool.driver import ContextSpec, Endpoint, LaunchSpec, WindowId
    from browser_pool.proxies import Proxy
    from browser_pool.state import SameSite

_logger = logging.getLogger(__name__)

type Channel = Literal["chrome", "edge"]

_PROXY_ERRORS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH",
    "ERR_INVALID_AUTH_CREDENTIALS",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
)
_PAGE_GONE = ("No target with given id", "Target closed", "Session with given id not found")
_BACKGROUND_FLAGS = (
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
)
"""Свёрнутые и перекрытые окна работают в полную силу — для отладки в окнах."""
_WATCH_INTERVAL = 0.5
"""Как часто сторож проверяет, жив ли процесс браузера, секунды."""
_LABEL_SCRIPT = """(label => {
  const apply = () => {
    if (!document.title.startsWith(label)) document.title = label + document.title;
  };
  const watch = () => {
    apply();
    const title = document.querySelector("title");
    if (title) new MutationObserver(apply).observe(title, {childList: true, characterData: true, subtree: true});
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", watch);
  else watch();
})"""
"""Префикс заголовка, который переживает смену заголовка сайтом."""
_SCREEN_SCRIPT = (
    "[screen.availLeft ?? 0, screen.availTop ?? 0, screen.availWidth, screen.availHeight]"
)


@dataclass(eq=False, slots=True)
class PydollContext:
    """Контекст браузера pydoll: браузер и номер контекста (`browserContextId`)."""

    browser: Browser
    """Браузер pydoll, в котором живёт контекст."""
    id: str | None
    """`browserContextId` — его принимают `new_tab`, `get_cookies`, `set_cookies` pydoll; `None` —
    готовый контекст браузера (профиль вендора, `reuse_default`)."""
    spec: ContextSpec = field(repr=False)
    """С чем контекст создан: эмуляция применяется к каждой его новой вкладке."""
    opener: Callable[[PydollContext], Awaitable[Tab]] | None = field(default=None, repr=False)
    """Как драйвер открывает вкладку контекста; задаёт драйвер."""

    async def new_tab(self) -> Tab:
        """Вкладка этого контекста — готовая: с авторизацией на прокси и эмуляцией identity.

        Для site SDK, который сам управляет вкладками (`pool.context`). Закрывает её тот, кто открыл.
        """
        if self.opener is None:
            msg = "Контекст создан не драйвером пула: открыть в нём готовую вкладку некому"
            raise RuntimeError(msg)
        return await self.opener(self)


class PydollDriver(BaseDriver["Browser", "PydollContext", "Tab"]):
    """Адаптер pydoll. Реализует `Driver[Browser, PydollContext, Tab]`."""

    def __init__(
        self,
        *,
        channel: Channel = "chrome",
        binary_location: str | None = None,
        arguments: Sequence[str] = (),
        start_timeout: int | None = None,
        headless: bool | None = None,
    ) -> None:
        """`channel` — Chrome или Edge; `binary_location` — свой бинарник вместо системного.

        `arguments` — флаги командной строки каждого браузера; `start_timeout` — сколько секунд
        pydoll ждёт, пока браузер ответит после запуска; `headless` — вместо решения пула (оно
        следует секции окон).
        """
        require_choice("PydollDriver.channel", channel, Channel)
        self.headless: bool | None = headless
        """Решение драйвера о режиме без окна; `None` — решает пул. Его читает менеджер окон."""
        self._channel: Channel = channel
        self._binary_location = binary_location
        self._arguments = list(arguments)
        self._start_timeout = start_timeout
        self._capabilities = DriverCapabilities(
            proxy_scope="context",
            can_new_context=True,
            state_support="cookies",
            persistent_dir=True,
            proxy_auth=True,
            proxy_schemes=frozenset({"http", "https", "socks5"}),
            proxy_auth_schemes=frozenset({"http", "https"}),  # на socks Chromium не авторизуется
            context_settings=CONTEXT_SETTINGS,
            # `slow_mo` pydoll не умеет: `Debug.slow_mo` с этим драйвером — предупреждение при старте.
            debug_options=frozenset({"keep_background_active"}),
            window_control="runtime",
        )
        self._pids: weakref.WeakKeyDictionary[Browser, int] = weakref.WeakKeyDictionary()
        self._owners: weakref.WeakKeyDictionary[Tab, Browser] = weakref.WeakKeyDictionary()
        self._launch_proxies: weakref.WeakKeyDictionary[Browser, Proxy] = (
            weakref.WeakKeyDictionary()
        )
        """Прокси, с которым запущен браузер (профиль на диске): его креды нужны каждой вкладке."""
        self._closed: weakref.WeakSet[Tab] = weakref.WeakSet()
        self._watchers: set[asyncio.Task[None]] = set()
        self._pools = 0
        """Сколько пулов сейчас работает на этом драйвере (`prepare` без `shutdown`)."""
        self._contexts: dict[str, PydollContext] = {}
        """Контексты пула по `browserContextId` — чтобы узнать свою вкладку по событию браузера."""
        self._pages: dict[str, tuple[weakref.ref[Tab], PydollContext]] = {}
        """Вкладки контекстов пула по `targetId`."""
        self._opening: dict[str, int] = {}
        """Сколько вкладок контекста драйвер открывает прямо сейчас: их событие — не чужая вкладка."""
        self._hints: weakref.WeakKeyDictionary[Browser, asyncio.Task[dict[str, Any] | None]] = (
            weakref.WeakKeyDictionary()
        )
        """Client hints браузера — снимаются один раз, по первой надобности."""
        self._adopting: set[asyncio.Task[None]] = set()

    @property
    @override
    def capabilities(self) -> DriverCapabilities:
        """Контекст на identity, прокси на контексте, состояние — куки, профиль на диске, окна на лету."""
        return self._capabilities

    # --- процесс SDK -------------------------------------------------------------------

    @override
    async def prepare(self) -> None:
        """У pydoll нет общего процесса: каждый браузер — свой Chrome. Считаются только пулы."""
        self._pools += 1

    @override
    async def shutdown(self) -> None:
        """Последний пул ушёл — останавливаются сторожа процессов; пока есть другие, они работают."""
        self._pools = max(0, self._pools - 1)
        if self._pools:
            return
        tasks = (*self._watchers, *self._adopting)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- браузер -----------------------------------------------------------------------

    @override
    async def launch(self, spec: LaunchSpec) -> Browser:
        """Запустить браузер: флаги драйвера, спецификации пула, её `extra` — атрибуты опций.

        С профилем (`spec.user_data_dir`) Chrome работает в нём, а не во временном каталоге pydoll:
        pydoll не создаёт и не удаляет каталог, если `--user-data-dir` уже задан.
        """
        browser = self._new_browser(spec)
        try:
            await browser.start()
            process = _process(browser)
            if process is None:
                # Процесс pydoll держит во внутреннем поле. Не нашли — значит, pydoll устроен иначе,
                # чем версии, с которыми драйвер проверен: молча работать без pid нельзя — зависший
                # браузер было бы нечем добить.
                msg = (
                    "PydollDriver не нашёл процесс запущенного браузера: эта версия pydoll устроена "
                    "иначе, чем проверенные (2.27–3.x)"
                )
                raise RuntimeError(msg)  # noqa: TRY301 — уборка та же, что при отмене запуска
            await self._observe(browser)
        except BaseException:
            # Запуск отменили (тайм-аут пула) или он не удался: Chrome уже мог стартовать.
            await _abandon(browser)
            raise
        self._pids[browser] = process.pid
        if spec.proxy is not None:
            self._launch_proxies[browser] = spec.proxy
        return browser

    @override
    async def attach(self, endpoint: Endpoint) -> Browser:
        """Подключиться к уже запущенному браузеру по CDP (адрес `ws://…/devtools/browser/…`)."""
        if endpoint.kind != "cdp":
            raise UnsupportedRequirementError(missing=(f"подключение {endpoint.kind}",))
        from pydoll.exceptions import NoValidTabFound

        browser = self._browser_class()()
        try:
            with contextlib.suppress(NoValidTabFound):  # вкладка пулу не нужна — он откроет свои
                await browser.connect(endpoint.url)
            await self._observe(browser)
        except BaseException:
            with contextlib.suppress(Exception):
                await browser.close()  # соединение, если успело открыться
            raise
        if endpoint.pid is not None:
            self._pids[browser] = endpoint.pid
        return browser

    @override
    async def ping(self, browser: Browser) -> bool:
        """Процесс жив и браузер отвечает по CDP."""
        if _dead(browser):
            return False
        await browser.get_version()
        return True

    @override
    def on_disconnect(self, browser: Browser, callback: Callable[[], None]) -> None:
        """Событий обрыва у pydoll нет: сторож ждёт выхода процесса. У подключённого — только ping."""
        process = _process(browser)
        if process is None:
            return
        task = asyncio.get_running_loop().create_task(_watch(process, callback))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    @override
    async def close_browser(self, browser: Browser) -> None:
        """`Browser.close` по CDP и ожидание выхода процесса — в потоке, не блокируя цикл."""
        process = _process(browser)
        if process is None:
            await browser.close()  # подключённый браузер принадлежит не нам: только соединение
            return
        from pydoll.commands import BrowserCommands

        if process.poll() is None:
            with contextlib.suppress(Exception):  # браузер уже не отвечает — дождёмся процесса
                await _cdp(browser, BrowserCommands.close())
            await asyncio.to_thread(process.wait)
        await _release(browser)

    @override
    async def kill_browser(self, browser: Browser) -> None:
        """Убить процесс браузера; у подключённого — закрыть соединение."""
        process = _process(browser)
        if process is None:
            await browser.close()
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        await asyncio.to_thread(process.wait)
        await _release(browser)

    @override
    def pid(self, browser: Browser) -> int | None:
        """Процесс браузера: у запущенного — от pydoll, у подключённого — из эндпоинта."""
        return self._pids.get(browser)

    # --- контекст ----------------------------------------------------------------------

    @override
    async def new_context(self, browser: Browser, spec: ContextSpec) -> PydollContext:
        """Контекст с прокси и куками; `reuse_default` — готовый контекст профиля вендора."""
        if spec.reuse_default:
            context_id = None
        else:
            # Без кредов: на авторизацию отвечает драйвер, на каждой вкладке (`_authorize`).
            proxy = spec.proxy.server if spec.proxy is not None else None
            context_id = await browser.create_browser_context(proxy_server=proxy)
        context = PydollContext(browser=browser, id=context_id, spec=spec, opener=self.new_page)
        if context_id is not None:
            self._contexts[context_id] = context
        try:
            if spec.state is not None and spec.state.cookies:
                await browser.set_cookies(_cookie_params(spec.state.cookies), context_id)
            if spec.geolocation is not None and context_id is not None:
                from pydoll.protocol.browser.types import PermissionType

                await browser.grant_permissions(
                    [PermissionType.GEOLOCATION], browser_context_id=context_id
                )
        except BaseException:
            await self.close_context(context)
            raise
        return context

    @override
    async def close_context(self, context: PydollContext) -> None:
        """Удалить контекст вместе с вкладками; у умершего браузера и у профиля вендора — нечего."""
        if context.id is None or self._contexts.pop(context.id, None) is None:
            return  # готовый контекст профиля — не наш; закрытый однажды второй раз не закрывается
        for target, (found, owner) in tuple(self._pages.items()):
            if owner is context:  # вкладки умирают вместе с контекстом — не дожидаясь события
                del self._pages[target]
                tab = found()
                if tab is not None:
                    self._closed.add(tab)
        if _dead(context.browser):
            return
        async with _unless_gone():
            await context.browser.delete_browser_context(context.id)

    @override
    async def export_state(self, context: PydollContext) -> SessionState:
        """Куки контекста плюс `extras`, с которыми он создан."""
        raw = await context.browser.get_cookies(context.id)
        state = context.spec.state
        cookies = [cookie for cookie in map(_from_cdp, raw) if cookie is not None]
        return SessionState(
            cookies=tuple(cookies), extras=state.extras if state is not None else {}
        )

    @override
    async def add_cookies(self, context: PydollContext, cookies: Sequence[Cookie]) -> None:
        """Добавить куки в живой контекст."""
        await context.browser.set_cookies(_cookie_params(cookies), context.id)

    # --- вкладки -----------------------------------------------------------------------

    @override
    async def new_page(self, context: PydollContext) -> Tab:
        """Вкладка в контексте — с авторизацией на прокси и эмуляцией identity."""
        key = context.id or ""
        self._opening[key] = self._opening.get(key, 0) + 1
        try:
            tab = await context.browser.new_tab(browser_context_id=context.id)
            self._register(tab, context)
        finally:
            self._opening[key] -= 1
            if not self._opening[key]:
                del self._opening[key]
        try:
            proxy = context.spec.proxy or self._launch_proxies.get(context.browser)
            if proxy is not None and proxy.has_auth:
                await _authorize(tab, proxy)
            await self._emulate(tab, context)
        except BaseException:
            await self.close_page(tab)
            raise
        return tab

    def _register(self, tab: Tab, context: PydollContext) -> None:
        self._owners[tab] = context.browser
        target = _target_id(tab)
        if target is not None:
            self._pages[target] = (weakref.ref(tab), context)

    async def _emulate(self, tab: Tab, context: PydollContext) -> None:
        spec = context.spec
        hints = None
        if spec.locale is not None and spec.user_agent is None and not spec.reuse_default:
            hints = await self._client_hints(context.browser)
        await _emulate(context.browser, tab, spec, hints=hints)

    async def _client_hints(self, browser: Browser) -> dict[str, Any] | None:
        """Настоящие client hints браузера: снимаются один раз, все вкладки ждут одного снятия."""
        task = self._hints.get(browser)
        if task is None:
            task = asyncio.get_running_loop().create_task(_read_client_hints(browser))
            self._hints[browser] = task
        return await asyncio.shield(task)

    # --- события браузера -------------------------------------------------------------------

    async def _observe(self, browser: Browser) -> None:
        """Слушать появление и исчезновение вкладок: закрытую не нами — знать, чужую — донастроить."""
        from pydoll.commands import TargetCommands

        def destroyed(event: dict[str, Any]) -> None:
            found = self._pages.pop(str(event["params"].get("targetId")), None)
            tab = found[0]() if found is not None else None
            if tab is not None:
                self._closed.add(tab)

        def created(event: dict[str, Any]) -> None:
            info = cast("dict[str, Any]", event["params"].get("targetInfo", {}))
            context = self._contexts.get(str(info.get("browserContextId")))
            target = str(info.get("targetId"))
            if info.get("type") != "page" or context is None or target in self._pages:
                return
            if self._opening.get(context.id or ""):
                return  # эту вкладку открывает сам драйвер: он её и настроит
            task = asyncio.get_running_loop().create_task(self._adopt(context, target))
            self._adopting.add(task)
            task.add_done_callback(self._adopting.discard)

        subscribe = cast("Any", browser).on  # перегрузки pydoll типизированы голым `dict`
        await subscribe("Target.targetDestroyed", destroyed)
        await subscribe("Target.targetCreated", created)
        discover = cast("Any", TargetCommands.set_discover_targets)  # `filter: list` без типа
        await _cdp(browser, discover(discover=True))

    async def _adopt(self, context: PydollContext, target: str) -> None:
        """Вкладка контекста, открытая в обход драйвера (site SDK, `window.open`): эмуляция — следом."""
        try:
            for tab in await context.browser.get_opened_tabs():
                if _target_id(tab) == target and target not in self._pages:
                    self._register(tab, context)
                    await self._emulate(tab, context)
        except Exception as error:  # noqa: BLE001 — вкладка могла уже закрыться: настраивать нечего
            _logger.debug("Чужая вкладка контекста не донастроена: %s", type(error).__name__)

    @override
    def page_usable(self, page: Tab) -> bool:
        """Вкладку не закрывали — ни драйвер, ни сайт, ни контекст, — и процесс браузера жив."""
        if page in self._closed:
            return False
        browser = self._owners.get(page)
        return browser is None or not _dead(browser)

    @override
    async def close_page(self, page: Tab) -> None:
        """Закрыть вкладку; закрытую кем-то ещё и вкладку умершего браузера закрывать нечего."""
        gone = page in self._closed
        self._closed.add(page)
        target = _target_id(page)
        if target is not None:
            self._pages.pop(target, None)
        browser = self._owners.get(page)
        if gone or (browser is not None and _dead(browser)):
            return
        async with _unless_gone():
            await page.close()

    @override
    async def capture(self, page: Tab) -> Evidence:
        """Снимок, разметка и адрес — что успелось; без исключений."""
        if not self.page_usable(page):
            return Evidence()
        screenshot: bytes | None = None
        html: str | None = None
        url: str | None = None
        try:
            url = await _read(page, "current_url")
            shot = await page.take_screenshot(as_base64=True, beyond_viewport=True)
            screenshot = base64.b64decode(shot) if shot is not None else None
            html = await _read(page, "page_source")
        except Exception as error:  # noqa: BLE001 — улики снимаются по возможности
            _logger.info("Снимок вкладки не снялся целиком: %s", type(error).__name__)
        return Evidence(screenshot=screenshot, html=html, url=url)

    # --- окна (WindowControl) ------------------------------------------------------------

    async def window_of(self, page: Tab) -> WindowId:
        """Окно вкладки — `windowId` CDP."""
        return await self._owner(page).get_window_id_for_tab(page)

    async def get_bounds(self, browser: Browser, window: WindowId) -> WindowBounds:
        """Где окно и в каком оно состоянии."""
        from pydoll.commands import BrowserCommands

        result = await _cdp(browser, BrowserCommands.get_window_bounds(int(window)))
        bounds = cast("dict[str, Any]", result["bounds"])
        return WindowBounds(
            rect=Rect(
                x=int(bounds["left"]),
                y=int(bounds["top"]),
                width=int(bounds["width"]),
                height=int(bounds["height"]),
            ),
            state=WindowState(bounds.get("windowState", "normal")),
        )

    async def set_bounds(self, browser: Browser, window: WindowId, bounds: WindowBounds) -> None:
        """Состояние и прямоугольник CDP не принимает вместе: сначала «обычное», потом место."""
        from pydoll.commands import BrowserCommands

        state = cast("Any", {"windowState": bounds.state.value})
        await _cdp(browser, BrowserCommands.set_window_bounds(int(window), state))
        if bounds.state is not WindowState.normal:
            return
        rect = bounds.rect
        place = cast(
            "Any", {"left": rect.x, "top": rect.y, "width": rect.width, "height": rect.height}
        )
        await _cdp(browser, BrowserCommands.set_window_bounds(int(window), place))

    async def screen_area(self, page: Tab) -> Rect:
        """Рабочая область монитора окна — `screen.avail*` страницы."""
        area = cast("list[int]", await _evaluate(page, _SCREEN_SCRIPT))
        return Rect(x=area[0], y=area[1], width=area[2], height=area[3])

    async def bring_to_front(self, page: Tab) -> None:
        """Поднять окно вкладки наверх."""
        await page.bring_to_front()

    async def label_page(self, page: Tab, label: str) -> None:
        """Префикс заголовка: для текущей страницы и для всех следующих навигаций вкладки."""
        from pydoll.commands import PageCommands

        source = f"{_LABEL_SCRIPT}({json.dumps(label)})"
        await _cdp(page, PageCommands.add_script_to_evaluate_on_new_document(source))
        await _evaluate(page, source)

    @override
    def classify(self, error: BaseException) -> ErrorKind | None:
        """Ошибки pydoll → вид сбоя: прокси, упавший браузер, вкладка.

        Сетевая ошибка ОС (`ConnectionRefusedError` и др.) — это браузер, только если её поднял
        слой соединений pydoll: вкладка переподключается к умершему браузеру. Такая же ошибка из
        кода приложения (свой HTTP-клиент) браузеру не приписывается.
        """
        from pydoll import exceptions

        if isinstance(error, OSError) and _raised_in(error, "pydoll.connection"):
            return ErrorKind.browser
        if _websocket_failure(error) and _raised_in(error, "pydoll"):
            # Вкладка переподключилась к цели, которой уже нет (закрыта сайтом или контекстом).
            return ErrorKind.page

        if isinstance(error, exceptions.NavigationError):
            proxy = any(code in error.error_text for code in _PROXY_ERRORS)
            return ErrorKind.proxy if proxy else ErrorKind.page
        if isinstance(
            error,
            exceptions.ConnectionException
            | exceptions.BrowserNotRunning
            | exceptions.FailedToStartBrowser,
        ):
            return ErrorKind.browser
        gone = isinstance(error, exceptions.CommandFailed) and any(
            text in str(error) for text in _PAGE_GONE
        )
        timeout = isinstance(
            error, exceptions.TimeoutException | exceptions.CommandExecutionTimeout
        )
        return ErrorKind.page if gone or timeout else None

    # --- внутреннее --------------------------------------------------------------------

    def _browser_class(self) -> type[Chrome | Edge]:
        from pydoll.browser import Chrome, Edge

        return Chrome if self._channel == "chrome" else Edge

    def _new_browser(self, spec: LaunchSpec) -> Browser:
        from pydoll.browser.options import ChromiumOptions

        options = ChromiumOptions()
        options.headless = spec.headless if self.headless is None else self.headless
        if self._binary_location is not None:
            options.binary_location = self._binary_location
        if self._start_timeout is not None:
            options.start_timeout = self._start_timeout
        for argument in _arguments(self._arguments, spec):
            if argument not in options.arguments:
                options.add_argument(argument)
        for name, value in spec.extra.items():
            setattr(options, name, value)
        browser = self._browser_class()(options=options)
        manager = getattr(browser, "_browser_process_manager", None)
        if manager is not None:
            manager._process_creator = _quiet_process  # noqa: SLF001 — см. докстринг модуля
        return browser

    def _owner(self, page: Tab) -> Browser:
        browser = self._owners.get(page)
        if browser is None:
            msg = "Вкладка открыта не этим драйвером: окно её браузера неизвестно"
            raise RuntimeError(msg)
        return browser


def _arguments(base: Sequence[str], spec: LaunchSpec) -> list[str]:
    arguments = [*base, *spec.args]
    if spec.window is not None:
        window = spec.window
        arguments += [
            f"--window-position={window.x},{window.y}",
            f"--window-size={window.width},{window.height}",
        ]
    if spec.keep_background_active:
        arguments += list(_BACKGROUND_FLAGS)
    if spec.proxy is not None:
        # Без кредов: на авторизацию отвечает драйвер, на каждой вкладке (`_authorize`).
        arguments.append(f"--proxy-server={spec.proxy.server}")
    if spec.user_data_dir is not None:
        arguments.append(f"--user-data-dir={spec.user_data_dir}")
    return arguments


async def _authorize(tab: Tab, proxy: Proxy) -> None:
    """Отвечать на запрос авторизации прокси на этой вкладке — пока она жива.

    Перехватываются только навигации (документы): первая же даёт прокси повод спросить креды, а
    дальше контекст браузера помнит их сам. Перехваченный запрос сразу отпускается. Один и тот же
    запрос, спросивший креды второй раз, — значит, прокси их не принял: второй раз они не
    предлагаются, и навигация падает ошибкой сети (вид сбоя — `proxy`), а не повторяет отказ до
    бесконечности и не показывает страницу 407 вместо сайта.
    """
    from pydoll.protocol.fetch.events import FetchEvent
    from pydoll.protocol.fetch.types import AuthChallengeResponseType
    from pydoll.protocol.network.types import ResourceType

    asked: set[str] = set()

    async def release(event: dict[str, Any]) -> None:
        with contextlib.suppress(
            Exception
        ):  # вкладка закрылась, пока запрос ждал: отпускать нечего
            await tab.continue_request(event["params"]["requestId"])

    async def answer(event: dict[str, Any]) -> None:
        params = event["params"]
        request = str(params["requestId"])
        by_proxy = params.get("authChallenge", {}).get("source") == "Proxy"
        # Не прокси (вход на сайт — дело site SDK) или прокси спросил второй раз: решает сетевой
        # стек браузера — без диалога он роняет навигацию `ERR_INVALID_AUTH_CREDENTIALS`.
        provide = by_proxy and request not in asked
        asked.add(request)
        with contextlib.suppress(Exception):
            await tab.continue_with_auth(
                request,
                AuthChallengeResponseType.PROVIDE_CREDENTIALS
                if provide
                else AuthChallengeResponseType.DEFAULT,
                proxy_username=proxy.username if provide else None,
                proxy_password=(proxy.password or "") if provide else None,
            )

    await tab.enable_fetch_events(handle_auth=True, resource_type=ResourceType.DOCUMENT)
    subscribe = cast("Any", tab).on  # перегрузки pydoll типизированы голым `dict`
    await subscribe(FetchEvent.REQUEST_PAUSED, release)
    await subscribe(FetchEvent.AUTH_REQUIRED, answer)


async def _emulate(
    browser: Browser, tab: Tab, spec: ContextSpec, *, hints: dict[str, Any] | None = None
) -> None:
    """Эмуляция identity на вкладке: действует до её закрытия, и после навигаций тоже.

    У готового контекста профиля (`reuse_default`) отпечаток вендора — его не трогаем.

    Локаль — это и `Intl` (`setLocaleOverride`), и `navigator.language` с `Accept-Language`
    (`setUserAgentOverride`); последний требует строку user agent — без своей берётся браузерная,
    и вместе с ней передаются настоящие client hints (`hints`): без них подмена их обнуляет.
    """
    from pydoll.commands import EmulationCommands

    if spec.reuse_default:
        return
    if spec.timezone is not None:
        await _cdp(tab, EmulationCommands.set_timezone_override(spec.timezone))
    if spec.locale is not None:
        await _cdp(tab, EmulationCommands.set_locale_override(spec.locale))
    if spec.geolocation is not None:
        geo = spec.geolocation
        await _cdp(
            tab,
            EmulationCommands.set_geolocation_override(
                latitude=geo.latitude, longitude=geo.longitude, accuracy=geo.accuracy or 1.0
            ),
        )
    if spec.viewport is not None:
        size = spec.viewport
        await _cdp(
            tab,
            EmulationCommands.set_device_metrics_override(
                width=size.width, height=size.height, device_scale_factor=0, mobile=False
            ),
        )
    if spec.user_agent is not None or spec.locale is not None:
        agent = spec.user_agent or (await browser.get_version())["userAgent"]
        await _cdp(
            tab,
            EmulationCommands.set_user_agent_override(
                agent, accept_language=spec.locale, user_agent_metadata=cast("Any", hints)
            ),
        )


_HINTS_SCRIPT = """(async () => {
  const data = navigator.userAgentData;
  if (!data) return null;
  const high = await data.getHighEntropyValues(
    ["architecture", "bitness", "model", "platformVersion", "fullVersionList", "wow64"]);
  return JSON.stringify({
    brands: data.brands, mobile: data.mobile, platform: data.platform,
    architecture: high.architecture, bitness: high.bitness, model: high.model,
    platformVersion: high.platformVersion, fullVersionList: high.fullVersionList, wow64: high.wow64,
  });
})()"""
_HINTS_PAGE = "chrome://version"
"""Страница браузера, которая грузится без сети и видит `navigator.userAgentData`: на пустой
вкладке (`about:blank`) его нет — это не защищённый контекст."""


async def _read_client_hints(browser: Browser) -> dict[str, Any] | None:
    """Client hints этого браузера — со служебной вкладки. Не вышло — `None`: эмуляция без них."""
    try:
        probe = await browser.new_tab()
        try:
            await probe.go_to(_HINTS_PAGE, timeout=10)
            response = cast(
                "dict[str, Any]",
                await probe.execute_script(_HINTS_SCRIPT, return_by_value=True, await_promise=True),
            )
            raw = cast("dict[str, Any]", response["result"]["result"]).get("value")
        finally:
            with contextlib.suppress(Exception):
                await probe.close()
        return cast("dict[str, Any]", json.loads(raw)) if isinstance(raw, str) else None
    except Exception as error:  # noqa: BLE001 — без client hints вкладка всё равно откроется
        _logger.warning(
            "Client hints браузера не снялись (%s): локаль контекста обнулит navigator.userAgentData",
            type(error).__name__,
        )
        return None


def _target_id(tab: Tab) -> str | None:
    """`targetId` вкладки — pydoll держит его во внутреннем поле."""
    target = getattr(tab, "_target_id", None)
    return target if isinstance(target, str) else None


def _websocket_failure(error: BaseException) -> bool:
    """Сбой веб-сокета (пакет `websockets`, на нём стоит pydoll)."""
    from websockets.exceptions import WebSocketException

    return isinstance(error, WebSocketException)


async def _abandon(browser: Browser) -> None:
    """Запуск не состоялся: добить процесс, если он успел появиться, и прибрать за pydoll."""
    process = _process(browser)
    if process is not None and process.poll() is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        await asyncio.to_thread(process.wait)
    await _release(browser)


async def _cdp(target: Browser | Tab, command: object) -> dict[str, Any]:
    """Команда CDP браузеру или вкладке; ответ — поле `result`.

    Публичного метода для произвольной команды у pydoll нет — внутренний `_execute_command`.
    """
    response = await target._execute_command(command)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage, reportArgumentType, reportUnknownVariableType]
    return cast("dict[str, Any]", cast("dict[str, Any]", response).get("result", {}))


async def _read(tab: Tab, name: str) -> str:
    """Строковое свойство вкладки: в pydoll 2 — awaitable-свойство, с pydoll 3 — метод."""
    attribute: Any = getattr(tab, name)
    pending = cast("Awaitable[object]", attribute() if callable(attribute) else attribute)
    return str(await pending)


def _raised_in(error: BaseException, package: str) -> bool:
    """Поднята ли ошибка кодом пакета `package` (по кадрам трассировки)."""
    trace = error.__traceback__
    while trace is not None:
        module = str(trace.tb_frame.f_globals.get("__name__", ""))
        if module == package or module.startswith(f"{package}."):
            return True
        trace = trace.tb_next
    return False


async def _evaluate(tab: Tab, script: str) -> object:
    response = cast("dict[str, Any]", await tab.execute_script(script, return_by_value=True))
    return cast("dict[str, Any]", response["result"]["result"]).get("value")


async def _watch(process: subprocess.Popen[bytes], callback: Callable[[], None]) -> None:
    """Дождаться выхода процесса браузера и сообщить об обрыве."""
    # Опрос, а не process.wait в потоке: поток исполнителя на каждый браузер кончился бы.
    while process.poll() is None:  # noqa: ASYNC110 — событие выхода процесса asyncio не даёт
        await asyncio.sleep(_WATCH_INTERVAL)
    callback()


async def _release(browser: Browser) -> None:
    """После выхода процесса: соединение и временный профиль (его удаление ждёт — в потоке)."""
    with contextlib.suppress(Exception):
        await browser.close()
    manager = getattr(browser, "_temp_directory_manager", None)
    if manager is not None:
        await asyncio.to_thread(manager.cleanup)


def _quiet_process(command: list[str]) -> subprocess.Popen[bytes]:
    """Процесс браузера без труб: вывод Chrome никто не читает."""
    return subprocess.Popen(  # noqa: S603 — команду собирает pydoll из опций драйвера
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _process(browser: Browser) -> subprocess.Popen[bytes] | None:
    """Процесс запущенного браузера; у подключённого — `None`. Pydoll держит его у себя."""
    manager = getattr(browser, "_browser_process_manager", None)
    return cast("subprocess.Popen[bytes] | None", getattr(manager, "_process", None))


@contextlib.asynccontextmanager
async def _unless_gone() -> AsyncGenerator[None]:
    """Закрытие у браузера, до которого нет связи (умирает прямо сейчас), — закрывать уже нечего."""
    from pydoll.exceptions import CommandFailed, ConnectionException

    try:
        yield
    # Вкладка pydoll переподключается сама, и отказ соединения выходит наружу как есть — в том
    # числе отказом веб-сокета, если цели уже нет (её закрыл сайт или контекст).
    except (ConnectionException, OSError) as error:
        _logger.debug("Браузер недоступен, закрывать нечего: %s", type(error).__name__)
    except CommandFailed as error:
        if not any(text in str(error) for text in _PAGE_GONE):
            raise
        _logger.debug("Цели уже нет, закрывать нечего")
    except Exception as error:
        if not _websocket_failure(error):
            raise
        _logger.debug("Цели уже нет, закрывать нечего: %s", type(error).__name__)


def _dead(browser: Browser) -> bool:
    """Процесс запущенного браузера завершился. Про подключённый не известно — считается живым."""
    process = _process(browser)
    return process is not None and process.poll() is not None


def _cookie_params(cookies: Sequence[Cookie]) -> list[CookieParam]:
    params: list[dict[str, Any]] = []
    for cookie in cookies:
        param: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "httpOnly": cookie.http_only,
        }
        if cookie.expires is not None:
            param["expires"] = cookie.expires.timestamp()
        if cookie.same_site is not None:
            param["sameSite"] = cookie.same_site
        params.append(param)
    return cast("list[CookieParam]", params)


_SAME_SITE: dict[str, SameSite] = {"strict": "Strict", "lax": "Lax", "none": "None"}


def _from_cdp(cookie: CdpCookie) -> Cookie | None:
    """Кука CDP → кука пула; `None` — модель её не представит (без имени): такая пропускается."""
    expires = cookie.get("expires", -1)
    # CDP отдаёт `sameSite` строкой («Lax»); перечисление pydoll — только аннотация, но и оно строка.
    raw: object = cookie.get("sameSite")
    same_site = _SAME_SITE.get(str(getattr(raw, "value", raw)).lower()) if raw is not None else None
    try:
        return Cookie(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie["domain"],
            path=cookie.get("path", "/"),
            expires=None
            if cookie.get("session", False) or expires <= 0
            else datetime.fromtimestamp(expires, UTC),
            secure=cookie.get("secure", False),
            http_only=cookie.get("httpOnly", False),
            same_site=same_site,
        )
    except ValueError as problem:
        # Одна странная кука сайта не должна стоить всего состояния сессии. Значение — не в лог.
        _logger.info(
            "Кука домена %s пропущена при снятии состояния: %s", cookie.get("domain"), problem
        )
        return None


__all__ = ["Channel", "PydollContext", "PydollDriver"]
