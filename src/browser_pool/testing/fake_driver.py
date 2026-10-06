"""Фейковый драйвер: браузер без браузера — для тестов ядра и для тестов приложений.

Ведёт себя как настоящий SDK в том, что важно пулу: контексты изолированы, закрытие
браузера закрывает всё внутри, падение браузера делает мёртвыми его вкладки и приходит
событием `disconnected` (оно же приходит и при штатном закрытии — как у настоящих SDK).

Возможности (`capabilities`) фейк соблюдает строго, как настоящий SDK соблюдал бы их по
необходимости: прокси контекста при `proxy_scope` не `"context"`, прокси запуска при
`"context"` (кроме браузера с профилем на диске), профиль без `persistent_dir`, второй контекст в
браузере без `can_new_context` и, при `thread_affinity`, вызов по браузеру не из того потока, в
котором он запущен, — `FakeDriverError`.

Профиль на диске (`LaunchSpec.user_data_dir`) — словарь кук в `profiles`: готовый контекст
браузера с профилем работает прямо с ним, поэтому вход переживает перезапуск браузера, как у
настоящего профиля — если его кука со сроком: сессионные куки новый запуск не видит.

Сверх этого — то, чего от настоящего браузера не добиться по заказу:

- сценарные сбои: `faults.fail("new_page", error)`, `faults.hang("close_browser")`,
  `faults.delay("launch", 5.0)` — на следующие вызовы операции;
- падение браузера в любой момент: `driver.crash(browser)`;
- журнал вызовов (`calls`) и счёт живых ресурсов (`live`);
- настоящий HTTP из вкладки — `await driver.fetch(page, url)` — с куками и прокси контекста;
- окна (`WindowControl`): у каждой вкладки своё окно, `screen` — рабочая область, `move_window`
  — «человек передвинул окно руками». Пул пользуется ими, только если в возможностях драйвера
  объявлено `window_control="runtime"`.

Упавший браузер считается живым, пока его не закрыли или не добили: процесс настоящего
браузера после падения тоже приходится прибирать. Его контексты и вкладки мертвы сразу.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NamedTuple, override

from browser_pool.driver import (
    CONTEXT_SETTINGS,
    DEBUG_OPTIONS,
    BaseDriver,
    ContextSpec,
    DriverCapabilities,
    Evidence,
    LaunchSpec,
    WindowBounds,
)
from browser_pool.errors import ErrorKind
from browser_pool.geometry import Rect
from browser_pool.proxies import SCHEMES
from browser_pool.state import SessionState
from browser_pool.testing.fake_http import FakeNetworkError, FakeProxyError, fetch

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from browser_pool.driver import Endpoint, WindowId
    from browser_pool.state import Cookie

type FakeOperation = Literal[
    "prepare",
    "shutdown",
    "launch",
    "attach",
    "ping",
    "close_browser",
    "kill_browser",
    "new_context",
    "close_context",
    "export_state",
    "add_cookies",
    "new_page",
    "close_page",
    "capture",
    "window_of",
    "get_bounds",
    "set_bounds",
    "screen_area",
    "bring_to_front",
    "label_page",
]
"""Асинхронные операции драйвера — те, на которые можно заказать сбой."""

DEFAULT_WINDOW = Rect(x=0, y=0, width=1280, height=720)
"""Где открывается новое окно фейка."""
DEFAULT_SCREEN = Rect(x=0, y=0, width=1920, height=1040)
"""Рабочая область фейкового монитора."""

FAKE_CAPABILITIES = DriverCapabilities(
    proxy_scope="context",
    can_new_context=True,
    fingerprint_scope="context",
    state_support="full",
    persistent_dir=True,
    proxy_auth=True,
    proxy_schemes=SCHEMES,
    context_settings=CONTEXT_SETTINGS,
    debug_options=DEBUG_OPTIONS,
)
"""Возможности по умолчанию — самый способный драйвер: много identity в одном браузере, прокси и
отпечаток на контексте, полное состояние, профиль на диске, прокси любой схемы с авторизацией.

Это больше, чем умеет любой настоящий драйвер поставки (у Playwright нет профиля на диске, у pydoll
состояние — только куки): приложение, которое опирается на возможность, проверяет её на
возможностях своего драйвера — `FakeDriver(capabilities=...)`."""

_ids = itertools.count(1)


class FakeDriverError(Exception):
    """Ошибка фейкового SDK."""


class FakeBrowserCrashedError(FakeDriverError):
    """Браузер упал — как `TargetClosedError` на мёртвом процессе."""


class FakeTargetClosedError(FakeDriverError):
    """Вкладка или контекст уже закрыты."""


@dataclass(eq=False, slots=True)
class FakeBrowser:
    """Браузер фейкового SDK."""

    spec: LaunchSpec | None = None
    endpoint: Endpoint | None = None
    id: int = field(default_factory=lambda: next(_ids))
    closed: bool = False
    crashed: bool = False
    thread: int = field(default_factory=threading.get_ident)
    """Поток, в котором браузер запущен: при `thread_affinity` все его вызовы — оттуда."""
    contexts: list[FakeContext] = field(default_factory=list["FakeContext"])
    listeners: list[Callable[[], None]] = field(default_factory=list["Callable[[], None]"])

    @property
    def alive(self) -> bool:
        """Отвечает ли браузер."""
        return not self.closed and not self.crashed


@dataclass(eq=False, slots=True)
class FakeContext:
    """Контекст фейкового SDK: свои куки, своё состояние."""

    browser: FakeBrowser
    spec: ContextSpec
    id: int = field(default_factory=lambda: next(_ids))
    closed: bool = False
    cookies: dict[tuple[str, str, str], Cookie] = field(
        default_factory=dict[tuple[str, str, str], "Cookie"], repr=False
    )
    state: SessionState = field(default_factory=SessionState)
    pages: list[FakePage] = field(default_factory=list["FakePage"])
    default: bool = False
    """Готовый контекст браузера (профиль вендора): `close_context` его не закрывает."""

    @property
    def alive(self) -> bool:
        """Жив ли контекст: не закрыт и браузер отвечает."""
        return not self.closed and self.browser.alive


@dataclass(eq=False, slots=True)
class FakeWindow:
    """Окно фейкового браузера."""

    id: int
    bounds: WindowBounds = field(default_factory=lambda: WindowBounds(rect=DEFAULT_WINDOW))
    fronted: int = 0
    """Сколько раз окно поднимали наверх."""


@dataclass(eq=False, slots=True)
class FakePage:
    """Вкладка фейкового SDK. Адрес можно менять напрямую — «навигация»."""

    context: FakeContext
    id: int = field(default_factory=lambda: next(_ids))
    closed: bool = False
    url: str = "about:blank"
    label: str | None = None
    """Подпись окна (`label_page`)."""
    window: FakeWindow = field(init=False)
    """Окно вкладки: у фейка у каждой вкладки своё."""

    def __post_init__(self) -> None:
        self.window = FakeWindow(id=self.id)

    @property
    def alive(self) -> bool:
        """Жива ли вкладка: не закрыта и контекст жив."""
        return not self.closed and self.context.alive


class FakeCall(NamedTuple):
    """Запись журнала: какая операция, над чем и из какого потока."""

    operation: FakeOperation
    target: object
    thread: int = 0


class LiveResources(NamedTuple):
    """Сколько ресурсов живо: браузеры (включая упавшие, но не прибранные), контексты, вкладки."""

    browsers: int
    contexts: int
    pages: int


@dataclass(slots=True)
class _Fault:
    """Один заказанный сбой: исключение, зависание или задержка."""

    error: BaseException | None = None
    hang: bool = False
    delay: float = 0.0


class FakeFaults:
    """Заказ сбоев на следующие вызовы операций. Заказы одной операции исполняются по очереди."""

    def __init__(self) -> None:
        self._queue: dict[FakeOperation, deque[_Fault]] = {}

    def fail(self, operation: FakeOperation, error: BaseException, *, times: int = 1) -> None:
        """Следующие `times` вызовов операции падают с `error`."""
        self._push(operation, _Fault(error=error), times)

    def hang(self, operation: FakeOperation, *, times: int = 1) -> None:
        """Следующие вызовы операции не возвращаются — пока их не отменят (таймаутом пула)."""
        self._push(operation, _Fault(hang=True), times)

    def delay(self, operation: FakeOperation, seconds: float, *, times: int = 1) -> None:
        """Следующие вызовы операции идут `seconds` секунд, а потом выполняются как обычно."""
        self._push(operation, _Fault(delay=seconds), times)

    def _push(self, operation: FakeOperation, fault: _Fault, times: int) -> None:
        if times < 1:
            msg = f"times должен быть ≥ 1, получено {times}"
            raise ValueError(msg)
        self._queue.setdefault(operation, deque()).extend([fault] * times)

    def take(self, operation: FakeOperation) -> _Fault | None:
        """Очередной заказ на операцию, если есть."""
        queue = self._queue.get(operation)
        return queue.popleft() if queue else None


class FakeDriver(BaseDriver[FakeBrowser, FakeContext, FakePage]):
    """Драйвер без браузера. Реализует `Driver[FakeBrowser, FakeContext, FakePage]`."""

    def __init__(self, *, capabilities: DriverCapabilities = FAKE_CAPABILITIES) -> None:
        self._capabilities = capabilities
        self.faults: FakeFaults = FakeFaults()
        """Заказ сбоев."""
        self.calls: list[FakeCall] = []
        """Журнал асинхронных вызовов — в порядке вызова, включая упавшие."""
        self.browsers: list[FakeBrowser] = []
        """Все браузеры, которые драйвер когда-либо запустил или подключил."""
        self.screen: Rect = DEFAULT_SCREEN
        """Рабочая область монитора, которую видят вкладки."""
        self.profiles: dict[Path, dict[tuple[str, str, str], Cookie]] = {}
        """Профили на диске: каталог → куки. Создаётся при первом запуске с профилем."""

    @property
    @override
    def capabilities(self) -> DriverCapabilities:
        """Возможности, с которыми драйвер создан."""
        return self._capabilities

    @property
    def live(self) -> LiveResources:
        """Сколько ресурсов сейчас живо. После корректной остановки пула — `(0, 0, 0)`."""
        browsers = [browser for browser in self.browsers if not browser.closed]
        contexts = [
            context for browser in browsers for context in browser.contexts if context.alive
        ]
        pages = [page for context in contexts for page in context.pages if page.alive]
        return LiveResources(len(browsers), len(contexts), len(pages))

    def crash(self, browser: FakeBrowser) -> None:
        """Браузер упал: вкладки и контексты мертвы, слушатели узнают об этом событием."""
        if not browser.alive:
            return
        browser.crashed = True
        self._notify(browser)

    # --- протокол Driver --------------------------------------------------------------

    @override
    async def prepare(self) -> None:
        """Подготовка на процесс: у фейка нечего готовить."""
        await self._enter("prepare", None)

    @override
    async def shutdown(self) -> None:
        """Освобождение после пула: у фейка нечего освобождать."""
        await self._enter("shutdown", None)

    @override
    async def launch(self, spec: LaunchSpec) -> FakeBrowser:
        """Запустить браузер."""
        await self._enter("launch", spec)
        if spec.user_data_dir is not None and not self._capabilities.persistent_dir:
            msg = "профиль на диске: драйвер без persistent_dir"
            raise FakeDriverError(msg)
        at_launch = self._capabilities.proxy_scope == "browser" or spec.user_data_dir is not None
        if spec.proxy is not None and not at_launch:
            msg = f"прокси при запуске: у драйвера proxy_scope={self._capabilities.proxy_scope!r}"
            raise FakeDriverError(msg)
        browser = FakeBrowser(spec=spec)
        self.browsers.append(browser)
        return browser

    @override
    async def attach(self, endpoint: Endpoint) -> FakeBrowser:
        """Подключиться к браузеру по эндпоинту."""
        await self._enter("attach", endpoint)
        browser = FakeBrowser(endpoint=endpoint)
        self.browsers.append(browser)
        return browser

    @override
    async def ping(self, browser: FakeBrowser) -> bool:
        """Отвечает ли браузер."""
        await self._enter("ping", browser)
        return browser.alive

    @override
    def on_disconnect(self, browser: FakeBrowser, callback: Callable[[], None]) -> None:
        """Позвать `callback`, когда браузер отвалится или закроется."""
        browser.listeners.append(callback)

    @override
    async def close_browser(self, browser: FakeBrowser) -> None:
        """Закрыть браузер и всё внутри. Упавший тоже прибирается."""
        await self._enter("close_browser", browser)
        self._close_browser(browser)

    @override
    async def kill_browser(self, browser: FakeBrowser) -> None:
        """Добить браузер — то же закрытие, но на него можно заказать отдельные сбои."""
        await self._enter("kill_browser", browser)
        self._close_browser(browser)

    @override
    def pid(self, browser: FakeBrowser) -> int | None:
        """Процесс браузера: у подключённого — из эндпоинта; у запущенного фейка процесса нет.

        Выдуманный номер опасен: страж процессов пула записал бы его в реестр и однажды добил
        бы настоящий процесс с тем же PID.
        """
        if browser.endpoint is not None:
            return browser.endpoint.pid
        return None

    @override
    async def new_context(self, browser: FakeBrowser, spec: ContextSpec) -> FakeContext:
        """Создать контекст со своими куками и состоянием из `spec.state`."""
        await self._enter("new_context", browser)
        self._require_alive(browser)
        if spec.reuse_default:
            return self._default_context(browser, spec)
        if spec.proxy is not None and self._capabilities.proxy_scope != "context":
            msg = f"прокси контекста: у драйвера proxy_scope={self._capabilities.proxy_scope!r}"
            raise FakeDriverError(msg)
        if browser.contexts and not self._capabilities.can_new_context:
            msg = f"второй контекст в браузере {browser.id}: драйвер без can_new_context"
            raise FakeDriverError(msg)
        state = spec.state if spec.state is not None else SessionState()
        context = FakeContext(browser=browser, spec=spec, state=state)
        _put_cookies(context, state.cookies)
        browser.contexts.append(context)
        return context

    @override
    async def close_context(self, context: FakeContext) -> None:
        """Закрыть контекст и его вкладки."""
        await self._enter("close_context", context)
        if not context.default:  # готовый контекст профиля принадлежит вендору
            context.closed = True

    @override
    async def export_state(self, context: FakeContext) -> SessionState:
        """Снять состояние: текущие куки плюс localStorage и extras, с которыми контекст создан."""
        await self._enter("export_state", context)
        self._require_open(context)
        return SessionState(
            cookies=tuple(context.cookies.values()),
            origins=context.state.origins,
            extras=context.state.extras,
        )

    @override
    async def add_cookies(self, context: FakeContext, cookies: Sequence[Cookie]) -> None:
        """Добавить куки; кука с тем же именем, доменом и путём заменяется."""
        await self._enter("add_cookies", context)
        self._require_open(context)
        _put_cookies(context, cookies)

    @override
    async def new_page(self, context: FakeContext) -> FakePage:
        """Открыть вкладку в контексте."""
        await self._enter("new_page", context)
        self._require_open(context)
        page = FakePage(context=context)
        context.pages.append(page)
        return page

    @override
    def page_usable(self, page: FakePage) -> bool:
        """Жива ли вкладка."""
        return page.alive

    @override
    async def close_page(self, page: FakePage) -> None:
        """Закрыть вкладку."""
        await self._enter("close_page", page)
        page.closed = True

    @override
    async def capture(self, page: FakePage) -> Evidence:
        """Снимок вкладки: у фейка — только адрес."""
        await self._enter("capture", page)
        return Evidence(url=page.url)

    async def fetch(self, page: FakePage, url: str) -> str:
        """«Открыть» адрес во вкладке: настоящий HTTP GET с куками и прокси контекста.

        Куки, которые поставил сайт, остаются в контексте; тело ответа возвращается текстом.
        Не вызов протокола драйвера — его зовёт тест, как site SDK зовёт `page.goto`.
        """
        context = page.context
        self._require_open(context)
        if page.closed:
            msg = f"вкладка {page.id} закрыта"
            raise FakeTargetClosedError(msg)
        proxy = context.spec.proxy
        if proxy is None and context.browser.spec is not None:
            proxy = context.browser.spec.proxy
        result = await asyncio.to_thread(
            fetch, url, proxy=proxy, cookies=tuple(context.cookies.values())
        )
        _put_cookies(context, result.cookies)
        page.url = url
        return result.body

    # --- окна (WindowControl) -----------------------------------------------------------

    async def window_of(self, page: FakePage) -> WindowId:
        """Окно вкладки: у фейка у каждой вкладки своё."""
        await self._enter("window_of", page)
        if not page.alive:
            msg = f"вкладка {page.id} закрыта"
            raise FakeTargetClosedError(msg)
        return page.window.id

    async def get_bounds(self, browser: FakeBrowser, window: WindowId) -> WindowBounds:
        """Где окно сейчас."""
        await self._enter("get_bounds", window)
        self._check_thread(browser)
        return self._window(browser, window).bounds

    async def set_bounds(
        self, browser: FakeBrowser, window: WindowId, bounds: WindowBounds
    ) -> None:
        """Поставить окно на место."""
        await self._enter("set_bounds", window)
        self._check_thread(browser)
        self._window(browser, window).bounds = bounds

    async def screen_area(self, page: FakePage) -> Rect:
        """Рабочая область монитора — `screen`."""
        await self._enter("screen_area", page)
        return self.screen

    async def bring_to_front(self, page: FakePage) -> None:
        """Поднять окно вкладки наверх."""
        await self._enter("bring_to_front", page)
        page.window.fronted += 1

    async def label_page(self, page: FakePage, label: str) -> None:
        """Подписать окно вкладки."""
        await self._enter("label_page", page)
        page.label = label

    def move_window(self, page: FakePage, rect: Rect) -> None:
        """Человек передвинул окно руками — не через пул."""
        page.window.bounds = WindowBounds(rect=rect, state=page.window.bounds.state)

    def _window(self, browser: FakeBrowser, window: WindowId) -> FakeWindow:
        self._require_alive(browser)
        for context in browser.contexts:
            for page in context.pages:
                if page.window.id == window and page.alive:
                    return page.window
        msg = f"окна {window} нет"
        raise FakeTargetClosedError(msg)

    @override
    def classify(self, error: BaseException) -> ErrorKind | None:
        """Свои ошибки фейк знает; чужие — нет."""
        if isinstance(error, FakeBrowserCrashedError):
            return ErrorKind.browser
        if isinstance(error, FakeProxyError):
            return ErrorKind.proxy
        if isinstance(error, FakeTargetClosedError | FakeNetworkError):
            return ErrorKind.page
        return None

    # --- внутреннее -------------------------------------------------------------------

    async def _enter(self, operation: FakeOperation, target: object) -> None:
        self.calls.append(FakeCall(operation, target, threading.get_ident()))
        browser = _browser_of(target)
        if browser is not None:
            self._check_thread(browser)
        fault = self.faults.take(operation)
        if fault is None:
            return
        if fault.delay:
            await asyncio.sleep(fault.delay)
        if fault.hang:
            await asyncio.get_running_loop().create_future()
        if fault.error is not None:
            raise fault.error

    def _close_browser(self, browser: FakeBrowser) -> None:
        if browser.closed:
            return
        was_alive = browser.alive
        browser.closed = True
        if was_alive:
            self._notify(browser)

    def _default_context(self, browser: FakeBrowser, spec: ContextSpec) -> FakeContext:
        """Готовый контекст браузера — один на браузер; куки состояния добавляются в него.

        У браузера с профилем на диске куки контекста — это куки профиля.
        """
        if spec.proxy is not None:
            msg = "прокси готового контекста задаёт вендор или запуск, а не пул"
            raise FakeDriverError(msg)
        context = next((context for context in browser.contexts if context.default), None)
        if context is None:
            context = FakeContext(browser=browser, spec=spec, default=True)
            profile = browser.spec.user_data_dir if browser.spec is not None else None
            if profile is not None:
                context.cookies = self._profile_cookies(profile)
            browser.contexts.append(context)
        if spec.state is not None:
            _put_cookies(context, spec.state.cookies)
        return context

    def _profile_cookies(self, profile: Path) -> dict[tuple[str, str, str], Cookie]:
        """Куки профиля при запуске: как у настоящего профиля, на диске только куки со сроком —
        сессионные умерли вместе с прошлым браузером."""
        kept = self.profiles.setdefault(profile, {})
        for key in [key for key, cookie in kept.items() if cookie.is_session]:
            del kept[key]
        return kept

    def _check_thread(self, browser: FakeBrowser) -> None:
        if self._capabilities.thread_affinity and threading.get_ident() != browser.thread:
            msg = f"вызов браузера {browser.id} не из его потока: драйвер с thread_affinity"
            raise FakeDriverError(msg)

    @staticmethod
    def _notify(browser: FakeBrowser) -> None:
        # Событие SDK приходит асинхронно, как у настоящего браузера, — не внутри вызова.
        loop = asyncio.get_running_loop()
        for listener in browser.listeners:
            loop.call_soon(listener)

    @staticmethod
    def _require_alive(browser: FakeBrowser) -> None:
        if browser.crashed:
            msg = f"браузер {browser.id} упал"
            raise FakeBrowserCrashedError(msg)
        if browser.closed:
            msg = f"браузер {browser.id} закрыт"
            raise FakeTargetClosedError(msg)

    def _require_open(self, context: FakeContext) -> None:
        self._require_alive(context.browser)
        if context.closed:
            msg = f"контекст {context.id} закрыт"
            raise FakeTargetClosedError(msg)


def _browser_of(target: object) -> FakeBrowser | None:
    """Браузер, к которому относится цель вызова, если она его часть."""
    if isinstance(target, FakeBrowser):
        return target
    if isinstance(target, FakeContext):
        return target.browser
    if isinstance(target, FakePage):
        return target.context.browser
    return None


def _put_cookies(context: FakeContext, cookies: Sequence[Cookie]) -> None:
    for cookie in cookies:
        context.cookies[cookie.name, cookie.domain, cookie.path] = cookie


__all__ = [
    "DEFAULT_SCREEN",
    "DEFAULT_WINDOW",
    "FAKE_CAPABILITIES",
    "FakeBrowser",
    "FakeBrowserCrashedError",
    "FakeCall",
    "FakeContext",
    "FakeDriver",
    "FakeDriverError",
    "FakeFaults",
    "FakeOperation",
    "FakePage",
    "FakeTargetClosedError",
    "FakeWindow",
    "LiveResources",
]
