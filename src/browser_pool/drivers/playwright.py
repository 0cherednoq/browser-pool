"""Драйвер Playwright: Chromium; Firefox и WebKit — не проверены.

Проверен контрактным набором и живыми прогонами только Chromium. `engine="firefox"` и `"webkit"`
драйвер запускает, но ими не проверялся: у них нет окон для отладки и неизвестен процесс
браузера (зависший добить нечем), а распознавание сбоев прокси по текстам их ошибок не сверено.

Много identity в одном браузере — каждая в своём `BrowserContext` со своим прокси и состоянием.
Playwright поднимает свой процесс (node) — `prepare` запускает его, `shutdown` останавливает
после последнего пула, который им пользуется.

    driver = PlaywrightDriver(headless=False, launch_options={"slow_mo": 50})
    async with BrowserPool(driver, config=config) as pool: ...

Опции SDK проходят насквозь: `launch_options` / `context_options` драйвера и `extra` спецификаций
(его заполняют хуки `before_launch` / `before_context`) уходят в `launch` / `new_context` как есть.

Процесс запущенного Chromium драйвер узнаёт у самого браузера (CDP `SystemInfo.getProcessInfo`):
Playwright его не отдаёт, а без него зависший браузер нечем добить.

Что сломалось, `classify` решает не только по тексту ошибки. Ошибка «цель закрыта» у Playwright одна
и для закрытой вкладки, и для умершего браузера — драйвер находит объект Playwright, на котором она
поднята (по трассировке), и смотрит, на связи ли его браузер. Тем же путём узнаётся контекст, прокси
которого не принял креды. По `http` браузер в этом случае молча показывает страницу 407 вместо
сайта — site SDK упал бы своей ошибкой, по которой пул прокси не сменит. Поэтому за прокси с логином
драйвер перехватывает навигации вкладки (CDP `Fetch`, только документы) и на запрос авторизации
прокси отвечает сам: креды, не принятые с первого раза, — сетевая ошибка навигации, вид сбоя —
`proxy`. Это Chromium и вкладки, которые открыл пул (включая временную вкладку `flow.open`);
вкладка, которую site SDK открыл сам (`context.new_page()`), авторизуется средствами Playwright.
У Firefox и WebKit вкладка с ответом 407 закрывается, и `proxy` — вид ошибки следующей операции.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast, override

from browser_pool._choice import require_choice
from browser_pool.driver import (
    CONTEXT_SETTINGS,
    DEBUG_OPTIONS,
    BaseDriver,
    DriverCapabilities,
    Evidence,
    WindowBounds,
    WindowState,
)
from browser_pool.errors import ErrorKind, UnsupportedRequirementError
from browser_pool.geometry import Rect
from browser_pool.procguard import kill_tree, process_token
from browser_pool.state import Cookie, Origin, SessionState

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence

    from playwright.async_api import (
        Browser,
        BrowserContext,
        BrowserType,
        Page,
        Playwright,
        ProxySettings,
        Response,
        StorageState,
    )

    from browser_pool.driver import ContextSpec, Endpoint, LaunchSpec, WindowId
    from browser_pool.proxies import Proxy

_logger = logging.getLogger(__name__)

type Engine = Literal["chromium", "firefox", "webkit"]

_PROXY_ERRORS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
    "NS_ERROR_PROXY",
    "NS_ERROR_UNKNOWN_PROXY_HOST",
)
_BROWSER_GONE = ("Browser has been closed", "browser has disconnected", "Browser closed")
_PAGE_GONE = ("Target crashed", "Page crashed", "Target page, context or browser has been closed")
_MS = 1000.0
_PROXY_AUTH_REQUIRED = 407
_POLL = 0.05
_BACKGROUND_FLAGS = (
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
)
"""Свёрнутые и перекрытые окна Chromium работают в полную силу — для отладки в окнах."""
_LABEL_SCRIPT = """label => {
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
}"""
"""Префикс заголовка, который переживает смену заголовка сайтом."""


class PlaywrightDriver(BaseDriver["Browser", "BrowserContext", "Page"]):
    """Адаптер Playwright. Реализует `Driver[Browser, BrowserContext, Page]`."""

    def __init__(
        self,
        *,
        engine: Engine = "chromium",
        launch_options: Mapping[str, Any] | None = None,
        context_options: Mapping[str, Any] | None = None,
        headless: bool | None = None,
    ) -> None:
        """`engine` — какой браузер Playwright запускать; опции — нативные, как в Playwright.

        `headless` — вместо решения пула: `None` — решает пул (окна для отладки включены — браузер
        с окном, иначе без), `True` / `False` — так и будет, что бы ни стояло в `launch_options`.
        """
        require_choice("PlaywrightDriver.engine", engine, Engine)
        self.headless: bool | None = headless
        """Решение драйвера о режиме без окна; `None` — решает пул. Его читает менеджер окон."""
        self._engine: Engine = engine
        self._launch_options = dict(launch_options or {})
        self._context_options = dict(context_options or {})
        chromium = engine == "chromium"
        self._capabilities = DriverCapabilities(
            proxy_scope="context",
            can_new_context=True,
            state_support="full",
            proxy_auth=True,
            proxy_schemes=frozenset({"http", "https", "socks5"}),
            proxy_auth_schemes=frozenset({"http", "https"}),  # на socks браузеры не авторизуются
            context_settings=CONTEXT_SETTINGS,
            # Флаги фоновых окон — флаги Chromium.
            debug_options=DEBUG_OPTIONS if chromium else frozenset({"slow_mo"}),
            # Окна двигает CDP — он есть только у Chromium; окно на вкладку — `window_per_page`.
            new_window=chromium,
            window_control="runtime" if chromium else "none",
        )
        self._playwright: Playwright | None = None
        self._users = 0
        self._lock = asyncio.Lock()
        self._pids: weakref.WeakKeyDictionary[Browser, int] = weakref.WeakKeyDictionary()
        self._tokens: weakref.WeakKeyDictionary[Browser, str] = weakref.WeakKeyDictionary()
        """Отпечаток процесса браузера (время создания): добивать — только тот самый процесс."""
        self._launch_proxies: weakref.WeakKeyDictionary[Browser, Proxy] = (
            weakref.WeakKeyDictionary()
        )
        self._proxy_rejected: weakref.WeakSet[BrowserContext] = weakref.WeakSet()
        """Контексты, прокси которых не принял креды: их ошибки — вида `proxy`."""
        self._closing: set[asyncio.Task[None]] = set()
        self._guarded: weakref.WeakKeyDictionary[BrowserContext, Proxy] = (
            weakref.WeakKeyDictionary()
        )
        """Контексты Chromium за прокси с логином: на авторизацию отвечают их вкладки сами."""
        self._extras: weakref.WeakKeyDictionary[BrowserContext, Mapping[str, Any]] = (
            weakref.WeakKeyDictionary()
        )
        self._borrowed: weakref.WeakSet[BrowserContext] = weakref.WeakSet()
        """Готовые контексты профилей вендора (`reuse_default`): их не закрываем."""
        self._windowed: weakref.WeakKeyDictionary[BrowserContext, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        """Контексты, чьи вкладки открываются каждая в своём окне (`window_per_page`), и замок:
        одновременные вкладки иначе все решили бы, что они первые, и открылись одним окном."""

    @property
    @override
    def capabilities(self) -> DriverCapabilities:
        """Контекст на identity, прокси на контексте, полное состояние сессии."""
        return self._capabilities

    # --- процесс SDK -------------------------------------------------------------------

    @override
    async def prepare(self) -> None:
        """Поднять Playwright, если он ещё не поднят."""
        async with self._lock:
            if self._playwright is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
            self._users += 1

    @override
    async def shutdown(self) -> None:
        """Последний пул остановился — остановить Playwright."""
        async with self._lock:
            self._users = max(0, self._users - 1)
            if self._users or self._playwright is None:
                return
            playwright, self._playwright = self._playwright, None
            await playwright.stop()

    # --- браузер -----------------------------------------------------------------------

    @override
    async def launch(self, spec: LaunchSpec) -> Browser:
        """Запустить браузер: опции драйвера, затем спецификации пула, затем её `extra`."""
        if spec.user_data_dir is not None:
            raise UnsupportedRequirementError(
                missing=("профиль на диске (user_data_dir): драйвер работает контекстами",)
            )
        options = self._launch_arguments(spec)
        browser = await self._browser_type().launch(**options)
        pid = await _browser_pid(browser)
        if pid is not None:
            self._pids[browser] = pid
            token = await asyncio.to_thread(process_token, pid)
            if token is not None:
                self._tokens[browser] = token
        if spec.proxy is not None:
            self._launch_proxies[browser] = spec.proxy
        return browser

    def _launch_arguments(self, spec: LaunchSpec) -> dict[str, Any]:
        """Опции запуска: драйвера, затем спецификации пула, затем её `extra`."""
        options: dict[str, Any] = {"headless": spec.headless, **self._launch_options}
        if self.headless is not None:
            options["headless"] = self.headless
        args = [*cast("list[str]", options.get("args", [])), *spec.args]
        if spec.window is not None and self._engine == "chromium":
            window = spec.window
            args += [
                f"--window-position={window.x},{window.y}",
                f"--window-size={window.width},{window.height}",
            ]
        if spec.keep_background_active and self._engine == "chromium":
            args += list(_BACKGROUND_FLAGS)
        if args:
            options["args"] = args
        if spec.slow_mo is not None:
            options.setdefault("slow_mo", spec.slow_mo)
        if spec.proxy is not None:
            options["proxy"] = _proxy_settings(spec.proxy)
        options.update(spec.extra)
        return options

    @override
    async def attach(self, endpoint: Endpoint) -> Browser:
        """Подключиться по CDP (Chromium) или к серверу Playwright."""
        match endpoint.kind:
            case "cdp":
                browser = await self._browser_type().connect_over_cdp(endpoint.url)
            case "playwright_ws":
                browser = await self._browser_type().connect(endpoint.url)
            case "webdriver":
                raise UnsupportedRequirementError(missing=("подключение по WebDriver",))
        if endpoint.pid is not None:
            self._pids[browser] = endpoint.pid
        return browser

    @override
    async def ping(self, browser: Browser) -> bool:
        """Соединение есть, а Chromium ещё и отвечает на запрос по CDP."""
        if not browser.is_connected():
            return False
        if browser.browser_type.name != "chromium":
            return True
        await _cdp(browser, "Browser.getVersion")
        return True

    @override
    def on_disconnect(self, browser: Browser, callback: Callable[[], None]) -> None:
        """Playwright сообщает об обрыве событием `disconnected`."""
        browser.on("disconnected", lambda _browser: callback())

    @override
    async def close_browser(self, browser: Browser) -> None:
        """Штатное закрытие."""
        await browser.close()

    @override
    async def kill_browser(self, browser: Browser) -> None:
        """Добить процесс браузера, если он известен; иначе — ещё раз закрыть соединение."""
        pid = self._pids.get(browser)
        if pid is None:
            await browser.close()
            return
        token = self._tokens.get(browser)
        if token is not None and await asyncio.to_thread(process_token, pid) != token:
            # Процесса уже нет, а его номер мог достаться чужому: по номеру не убиваем.
            _logger.info("Процесс браузера %d уже завершился", pid)
            return
        await asyncio.to_thread(kill_tree, pid)

    @override
    def pid(self, browser: Browser) -> int | None:
        """Процесс браузера: у запущенного Chromium и у подключённого с известным `pid`."""
        return self._pids.get(browser)

    # --- контекст ----------------------------------------------------------------------

    @override
    async def new_context(self, browser: Browser, spec: ContextSpec) -> BrowserContext:
        """Контекст с прокси, состоянием и настройками identity; `reuse_default` — готовый профиль."""
        if spec.reuse_default:
            return await self._default_context(browser, spec)
        options: dict[str, Any] = {**self._context_options, **_context_options(spec)}
        granted = cast("list[str]", self._context_options.get("permissions", []))
        if granted and spec.geolocation is not None:
            # Геопозиция identity добавляет своё разрешение к разрешениям драйвера, а не заменяет их.
            options["permissions"] = [*dict.fromkeys([*granted, "geolocation"])]
        options.update(spec.extra)
        context = await browser.new_context(**options)
        if spec.default_timeout is not None:
            context.set_default_timeout(spec.default_timeout * _MS)
        proxy = spec.proxy or self._launch_proxies.get(browser)
        if proxy is not None and proxy.has_auth:
            if self._engine == "chromium":
                self._guarded[context] = proxy
            else:
                context.on("response", lambda response: self._watch_proxy_auth(context, response))
        if spec.state is not None and spec.state.extras:
            self._extras[context] = spec.state.extras
        if spec.window_per_page and self._engine == "chromium":
            self._windowed[context] = asyncio.Lock()
        return context

    @override
    async def close_context(self, context: BrowserContext) -> None:
        """Закрыть контекст и его вкладки; готовый контекст профиля остаётся вендору."""
        if context in self._borrowed:
            return
        await context.close()

    @override
    async def export_state(self, context: BrowserContext) -> SessionState:
        """Куки и localStorage контекста плюс `extras`, с которыми он создан."""
        raw = await context.storage_state()
        return _from_storage_state(raw, extras=self._extras.get(context, {}))

    @override
    async def add_cookies(self, context: BrowserContext, cookies: Sequence[Cookie]) -> None:
        """Добавить куки в живой контекст."""
        await context.add_cookies([_cookie_param(cookie) for cookie in cookies])  # pyright: ignore[reportArgumentType] — TypedDict SDK собирается динамически

    # --- вкладки -----------------------------------------------------------------------

    @override
    async def new_page(self, context: BrowserContext) -> Page:
        """Открыть вкладку; у контекста с `window_per_page` — в новом окне.

        Видимый Chromium открывает страницы контекста вкладками одного окна. Отдельное окно
        Playwright не умеет — его просит CDP (`Target.createTarget(newWindow=true)`) в том же
        контексте, а Playwright подхватывает страницу событием `page`.
        """
        page = await self._open_page(context)
        proxy = self._guarded.get(context)
        if proxy is not None:
            try:
                await self._guard_proxy_auth(context, page, proxy)
            except BaseException:
                await _close_quietly(page)
                raise
        return page

    async def _open_page(self, context: BrowserContext) -> Page:
        lock = self._windowed.get(context)
        if lock is None or context.browser is None:
            return await context.new_page()
        async with lock:
            if not context.pages:
                return await context.new_page()  # первая вкладка — своё окно у контекста и так
            return await _page_in_new_window(context.browser, context)

    async def _guard_proxy_auth(self, context: BrowserContext, page: Page, proxy: Proxy) -> None:
        """Креды, которые прокси не принял, — сетевая ошибка навигации, а не «страница сайта».

        Chromium, CDP `Fetch` на вкладке, только документы. На запрос авторизации прокси вкладка
        отвечает кредами сама; тот же запрос, спросивший второй раз, — отказ: авторизация
        отменяется, а пришедший следом ответ 407 обрывается ошибкой сети.
        """
        session = await context.new_cdp_session(page)
        asked: set[str] = set()
        rejected: set[str] = set()

        async def send(method: str, params: dict[str, Any]) -> None:
            try:
                await session.send(method, params)  # pyright: ignore[reportUnknownMemberType]
            except Exception as error:  # noqa: BLE001 — вкладка закрылась, пока запрос ждал
                _logger.debug("Перехваченный запрос не отпущен: %s", type(error).__name__)

        async def paused(event: dict[str, Any]) -> None:
            request = str(event["requestId"])
            if request in rejected and event.get("responseStatusCode") == _PROXY_AUTH_REQUIRED:
                await send(
                    "Fetch.failRequest", {"requestId": request, "errorReason": "AccessDenied"}
                )
            else:
                await send("Fetch.continueRequest", {"requestId": request})

        async def challenged(event: dict[str, Any]) -> None:
            request = str(event["requestId"])
            answer: dict[str, Any] = {"response": "Default"}  # вход на сайт — дело site SDK
            if cast("dict[str, Any]", event.get("authChallenge", {})).get("source") == "Proxy":
                if request in asked:
                    rejected.add(request)
                    self._proxy_rejected.add(context)
                    answer = {"response": "CancelAuth"}
                else:
                    asked.add(request)
                    answer = {
                        "response": "ProvideCredentials",
                        "username": proxy.username or "",
                        "password": proxy.password or "",
                    }
            await send(
                "Fetch.continueWithAuth", {"requestId": request, "authChallengeResponse": answer}
            )

        def spawn(
            handler: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
        ) -> Callable[[dict[str, Any]], None]:
            def run(event: dict[str, Any]) -> None:
                task = asyncio.get_running_loop().create_task(handler(event))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

            return run

        session.on("Fetch.requestPaused", spawn(paused))
        session.on("Fetch.authRequired", spawn(challenged))
        document = {"urlPattern": "*", "resourceType": "Document"}
        patterns = [{**document, "requestStage": stage} for stage in ("Request", "Response")]
        await session.send("Fetch.enable", {"handleAuthRequests": True, "patterns": patterns})  # pyright: ignore[reportUnknownMemberType]

    def _watch_proxy_auth(self, context: BrowserContext, response: Response) -> None:
        """Прокси ответил 407 — креды не приняты: вкладка закрывается, ошибка её операций — `proxy`.

        Для движков без CDP (Firefox, WebKit): перехватить ответ навигации там нечем.
        """
        if response.status != _PROXY_AUTH_REQUIRED:
            return
        self._proxy_rejected.add(context)
        page = response.frame.page
        if page.is_closed():
            return
        task = asyncio.get_running_loop().create_task(_close_quietly(page))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    @override
    def page_usable(self, page: Page) -> bool:
        """Вкладка не закрыта и браузер на связи."""
        if page.is_closed():
            return False
        browser = page.context.browser
        return browser is None or browser.is_connected()

    @override
    async def close_page(self, page: Page) -> None:
        """Закрыть вкладку."""
        await page.close()

    # --- окна (WindowControl, только Chromium) ------------------------------------------

    async def window_of(self, page: Page) -> WindowId:
        """Окно вкладки — `windowId` CDP."""
        session = await page.context.new_cdp_session(page)
        try:
            result = cast("dict[str, Any]", await session.send("Browser.getWindowForTarget"))  # pyright: ignore[reportUnknownMemberType]
        finally:
            await session.detach()
        return int(result["windowId"])

    async def get_bounds(self, browser: Browser, window: WindowId) -> WindowBounds:
        """Где окно и в каком оно состоянии."""
        result = await _cdp(browser, "Browser.getWindowBounds", {"windowId": window})
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
        state = {"windowState": bounds.state.value}
        await _cdp(browser, "Browser.setWindowBounds", {"windowId": window, "bounds": state})
        if bounds.state is not WindowState.normal:
            return
        rect = bounds.rect
        place = {"left": rect.x, "top": rect.y, "width": rect.width, "height": rect.height}
        await _cdp(browser, "Browser.setWindowBounds", {"windowId": window, "bounds": place})

    async def screen_area(self, page: Page) -> Rect:
        """Рабочая область монитора окна — `screen.avail*` страницы."""
        area = cast(
            "list[int]",
            await page.evaluate(
                "() => [screen.availLeft ?? 0, screen.availTop ?? 0, screen.availWidth, screen.availHeight]"
            ),
        )
        return Rect(x=area[0], y=area[1], width=area[2], height=area[3])

    async def bring_to_front(self, page: Page) -> None:
        """Поднять окно вкладки наверх."""
        await page.bring_to_front()

    async def label_page(self, page: Page, label: str) -> None:
        """Префикс заголовка: для текущей страницы и для всех следующих навигаций вкладки."""
        await page.add_init_script(f"({_LABEL_SCRIPT})({json.dumps(label)})")
        await page.evaluate(_LABEL_SCRIPT, label)

    @override
    async def capture(self, page: Page) -> Evidence:
        """Снимок, разметка и адрес — что успелось; без исключений."""
        screenshot: bytes | None = None
        html: str | None = None
        url: str | None = None
        # Каждая часть — сама по себе: упавший снимок не лишает разметки.
        try:
            url = page.url
        except Exception as error:  # noqa: BLE001 — улики снимаются по возможности
            _logger.info("Адрес вкладки не снялся: %s", type(error).__name__)
        try:
            screenshot = await page.screenshot(full_page=True)
        except Exception as error:  # noqa: BLE001
            _logger.info("Снимок вкладки не снялся: %s", type(error).__name__)
        try:
            html = await page.content()
        except Exception as error:  # noqa: BLE001
            _logger.info("Разметка вкладки не снялась: %s", type(error).__name__)
        return Evidence(screenshot=screenshot, html=html, url=url)

    @override
    def classify(self, error: BaseException) -> ErrorKind | None:
        """Ошибки Playwright → вид сбоя: прокси, упавший браузер, вкладка."""
        from playwright.async_api import Error
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        if not isinstance(error, Error):
            return None
        message = error.message
        context, browser = _origin_of(error)
        rejected = context is not None and context in self._proxy_rejected
        if rejected or any(code in message for code in _PROXY_ERRORS):
            return ErrorKind.proxy
        gone = any(text in message for text in _PAGE_GONE)
        # «Цель закрыта» Playwright говорит и про вкладку, и про умерший браузер: различает их
        # только то, на связи ли браузер объекта, на котором ошибка поднята.
        dead = gone and browser is not None and not browser.is_connected()
        if dead or any(text in message for text in _BROWSER_GONE):
            return ErrorKind.browser
        page_fault = (
            gone
            or isinstance(error, PlaywrightTimeoutError)
            or "net::ERR_" in message
            or "NS_ERROR_" in message
        )
        return ErrorKind.page if page_fault else None

    # --- внутреннее --------------------------------------------------------------------

    async def _default_context(self, browser: Browser, spec: ContextSpec) -> BrowserContext:
        """Готовый контекст браузера (`contexts[0]` у подключённого по CDP профиля вендора)."""
        if not browser.contexts:
            raise UnsupportedRequirementError(
                missing=("готовый контекст браузера: у подключённого браузера его нет",)
            )
        context = browser.contexts[0]
        self._borrowed.add(context)
        if spec.state is not None:
            if spec.state.cookies:
                await self.add_cookies(context, spec.state.cookies)
            if spec.state.extras:
                self._extras[context] = spec.state.extras
        return context

    def _browser_type(self) -> BrowserType:
        if self._playwright is None:
            msg = "PlaywrightDriver не подготовлен: пул зовёт prepare() при старте"
            raise RuntimeError(msg)
        return cast("BrowserType", getattr(self._playwright, self._engine))


def _proxy_settings(proxy: Proxy) -> ProxySettings:
    settings: ProxySettings = {"server": proxy.server}
    if proxy.username is not None:
        settings["username"] = proxy.username
    if proxy.password is not None:
        settings["password"] = proxy.password
    return settings


async def _close_quietly(page: Page) -> None:
    try:
        await page.close()
    except Exception as error:  # noqa: BLE001 — вкладка уже закрыта или браузер ушёл
        _logger.debug("Вкладка за отвергнувшим креды прокси не закрылась: %s", type(error).__name__)


def _origin_of(error: BaseException) -> tuple[BrowserContext | None, Browser | None]:
    """Контекст и браузер, на объекте которых поднята ошибка Playwright, — по трассировке.

    В ошибке Playwright нет ссылки на вкладку, но в кадрах его публичного API (`async_api`) лежит
    `self` — вкладка, локатор, фрейм, контекст или браузер, чей метод звали.
    """
    from playwright.async_api import Browser, BrowserContext

    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        trace = trace.tb_next
        if not str(frame.f_globals.get("__name__", "")).startswith("playwright.async_api"):
            continue
        owner: object = frame.f_locals.get("self")
        for _ in range(4):  # локатор → вкладка → контекст → браузер
            if isinstance(owner, Browser):
                return None, owner
            if isinstance(owner, BrowserContext):
                return owner, owner.browser
            owner = next(
                (found for name in ("context", "page", "owner") if (found := _peek(owner, name))),
                None,
            )
    return None, None


def _peek(owner: object, name: str) -> object:
    """Свойство объекта Playwright; у мёртвого объекта оно может бросить — тогда `None`."""
    try:
        return cast("object", getattr(owner, name, None))
    except Exception:  # noqa: BLE001 — свойство мёртвого объекта
        return None


def _context_options(spec: ContextSpec) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if spec.proxy is not None:
        options["proxy"] = _proxy_settings(spec.proxy)
    if spec.state is not None:
        options["storage_state"] = _to_storage_state(spec.state)
    if spec.locale is not None:
        options["locale"] = spec.locale
    if spec.timezone is not None:
        options["timezone_id"] = spec.timezone
    if spec.geolocation is not None:
        geo = spec.geolocation
        options["geolocation"] = {"latitude": geo.latitude, "longitude": geo.longitude}
        if geo.accuracy is not None:
            options["geolocation"]["accuracy"] = geo.accuracy
        options["permissions"] = ["geolocation"]
    if spec.viewport is not None:
        options["viewport"] = {"width": spec.viewport.width, "height": spec.viewport.height}
    if spec.user_agent is not None:
        options["user_agent"] = spec.user_agent
    if spec.fit_window and spec.viewport is None:
        options["no_viewport"] = True  # страница следует за окном, а не 1280×720 в маленьком окне
    return options


def _cookie_param(cookie: Cookie) -> dict[str, Any]:
    param: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path,
        "secure": cookie.secure,
        "httpOnly": cookie.http_only,
        "expires": cookie.expires.timestamp() if cookie.expires is not None else -1,
    }
    if cookie.same_site is not None:
        param["sameSite"] = cookie.same_site
    return param


def _to_storage_state(state: SessionState) -> StorageState:
    return cast(
        "StorageState",
        {
            "cookies": [_cookie_param(cookie) for cookie in state.cookies],
            "origins": [
                {
                    "origin": origin.origin,
                    "localStorage": [
                        {"name": name, "value": value} for name, value in origin.local_storage
                    ],
                }
                for origin in state.origins
            ],
        },
    )


def _from_storage_state(raw: StorageState, *, extras: Mapping[str, Any]) -> SessionState:
    cookies: list[Cookie] = []
    for cookie in raw.get("cookies", []):
        try:
            cookies.append(
                Cookie(
                    name=cookie.get("name", ""),
                    value=cookie.get("value", ""),
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                    expires=_expires(cookie.get("expires", -1)),
                    secure=cookie.get("secure", False),
                    http_only=cookie.get("httpOnly", False),
                    same_site=cookie.get("sameSite"),
                )
            )
        except ValueError as problem:
            # Кука без имени или с неизвестным атрибутом: браузеры такие хранят. Одна странная кука
            # сайта не должна стоить всего состояния сессии. Значение в лог не попадает.
            _logger.info(
                "Кука домена %s пропущена при снятии состояния: %s", cookie.get("domain"), problem
            )
    origins = tuple(
        Origin(
            origin=origin["origin"],
            local_storage=tuple(
                (entry["name"], entry["value"]) for entry in origin["localStorage"]
            ),
        )
        for origin in raw.get("origins", [])
    )
    return SessionState(cookies=tuple(cookies), origins=origins, extras=extras)


def _expires(seconds: float) -> datetime | None:
    """Playwright пишет срок секундами эпохи, а сессионную куку — `-1`."""
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


async def _page_in_new_window(browser: Browser, context: BrowserContext) -> Page:
    """Страница контекста в новом окне: CDP `Target.createTarget(newWindow=true)`."""
    session = await context.new_cdp_session(context.pages[0])
    try:
        info = cast("dict[str, Any]", await session.send("Target.getTargetInfo"))  # pyright: ignore[reportUnknownMemberType]
    finally:
        await session.detach()
    params = {
        "url": "about:blank",
        "browserContextId": info["targetInfo"]["browserContextId"],
        "newWindow": True,
    }
    known = set(context.pages)
    target = str((await _cdp(browser, "Target.createTarget", params))["targetId"])
    # Не «первая новая страница контекста»: одновременно попап мог открыть и сайт — ищем свою цель.
    while True:
        for page in context.pages:
            if page not in known and await _target_of(context, page) == target:
                await page.wait_for_load_state()
                return page
        await asyncio.sleep(_POLL)  # срок ожидания — тайм-аут пула на создание вкладки


async def _target_of(context: BrowserContext, page: Page) -> str | None:
    """`targetId` страницы; у закрывшейся — `None`."""
    try:
        session = await context.new_cdp_session(page)
        try:
            info = cast("dict[str, Any]", await session.send("Target.getTargetInfo"))  # pyright: ignore[reportUnknownMemberType]
        finally:
            await session.detach()
    except Exception:  # noqa: BLE001 — страница закрылась, пока спрашивали
        return None
    return str(info["targetInfo"]["targetId"])


async def _cdp(
    browser: Browser, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Один запрос CDP к браузеру (только Chromium)."""
    session = await browser.new_browser_cdp_session()
    try:
        # Стабы Playwright описывают ответ как голый Dict — тип ответа CDP знает вызывающий.
        result = await session.send(method, params)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    finally:
        await session.detach()
    return cast("dict[str, Any]", result)


async def _browser_pid(browser: Browser) -> int | None:
    """Процесс запущенного Chromium — по CDP. Не вышло — `None`: добивать будет нечем."""
    if browser.browser_type.name != "chromium":
        return None
    try:
        info = await _cdp(browser, "SystemInfo.getProcessInfo")
    except Exception as error:  # noqa: BLE001 — pid нужен только для добивания
        _logger.info("Процесс браузера не определился: %s", type(error).__name__)
        return None
    processes = cast("list[dict[str, Any]]", info.get("processInfo", []))
    return next(
        (int(process["id"]) for process in processes if process.get("type") == "browser"), None
    )


__all__ = ["Engine", "PlaywrightDriver"]
