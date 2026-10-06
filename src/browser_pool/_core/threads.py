"""Поток на браузер для синхронных SDK: `thread_affinity`.

Синхронный SDK (Selenium, undetected-chromedriver) блокирует и не терпит вызовов одного
браузера из разных потоков. Ядро при этом — только asyncio. `ThreadBoundDriver` оборачивает
такой драйвер: каждый запущенный браузер получает свой поток со своим циклом событий, и все
асинхронные вызовы драйвера по этому браузеру, его контекстам и вкладкам идут в этом потоке.
Там же исполняется sync-код арендатора — `lease.call(fn, *args)` — и flow — `ctx.call(...)`.
Подготовка драйвера (`prepare`/`shutdown`) — в отдельном служебном потоке.

Цикл пула в это время не блокируется: ожидание ответа потока идёт через исполнитель цикла.

Синхронные методы протокола (`pid`, `page_usable`, `on_disconnect`, `classify`) пул зовёт из
своего потока, поэтому драйвер с `thread_affinity` отвечает на них без обращения к SDK — по
своему состоянию. `pid` обёртка снимает в потоке браузера сразу после запуска и помнит сама;
колбэк `on_disconnect`, откуда бы SDK его ни позвал, приходит в цикл пула.

Зависший вызов потока не прервать: отмена ожидания только отказывается от результата. Браузер
с зависшим потоком добивает страж процессов пула — после этого вызов SDK падает и поток
освобождается.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import threading
from typing import TYPE_CHECKING, Any, cast

from browser_pool.driver import PageLabeler, WindowControl
from browser_pool.errors import PoolInvariantError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from browser_pool.driver import (
        ContextSpec,
        Driver,
        DriverCapabilities,
        Endpoint,
        Evidence,
        LaunchSpec,
        WindowBounds,
        WindowId,
    )
    from browser_pool.errors import ErrorKind
    from browser_pool.geometry import Rect
    from browser_pool.state import Cookie, SessionState

_names = itertools.count(1)


class BrowserThread:
    """Поток со своим циклом событий: в нём идут все вызовы SDK для одного браузера."""

    def __init__(self, name: str) -> None:
        # ast-grep-ignore: library-does-not-own-event-loop — цикл своего потока, не цикл приложения
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)
        self._thread.start()

    @property
    def ident(self) -> int | None:
        """Номер потока ОС-уровня Python (`threading.get_ident()` внутри него)."""
        return self._thread.ident

    async def run[T](self, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Исполнить корутину в потоке и дождаться результата, не блокируя цикл пула."""
        future = asyncio.run_coroutine_threadsafe(factory(), self._loop)
        try:
            # Через исполнитель: цикл пула (и виртуальное время в тестах) знает, что ждёт поток.
            return await asyncio.get_running_loop().run_in_executor(None, future.result)
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def call[**A, T](self, fn: Callable[A, T], *args: A.args, **kwargs: A.kwargs) -> T:
        """Исполнить синхронную функцию в потоке."""

        async def invoke() -> T:
            return fn(*args, **kwargs)

        return await self.run(invoke)

    def stop(self) -> None:
        """Остановить цикл потока, когда он доделает начатое. Не ждёт."""
        with contextlib.suppress(RuntimeError):  # цикл уже закрыт
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()


class ThreadBoundDriver[B, C, P]:
    """Драйвер синхронного SDK, у которого каждый браузер живёт в своём потоке.

    Реализует `Driver[B, C, P]`; `bind_threads` добавляет управление окнами и подписи, если их
    умеет исходный драйвер.
    """

    def __init__(self, driver: Driver[B, C, P]) -> None:
        self.driver: Driver[B, C, P] = driver
        """Исходный драйвер."""
        self._threads: dict[int, tuple[object, BrowserThread]] = {}
        """Объект SDK (по `id`, со ссылкой, чтобы номер не переиспользовался) → поток его браузера."""
        self._pids: dict[int, int | None] = {}
        self._service: BrowserThread | None = None

    @property
    def capabilities(self) -> DriverCapabilities:
        """Возможности исходного драйвера."""
        return self.driver.capabilities

    def thread_of(self, target: object) -> BrowserThread:
        """Поток браузера, к которому относится браузер, контекст или вкладка."""
        entry = self._threads.get(id(target))
        if entry is None or entry[0] is not target:
            msg = f"{type(target).__name__} не принадлежит ни одному браузеру пула"
            raise PoolInvariantError(msg)
        return entry[1]

    async def call[**A, T](
        self, target: object, fn: Callable[A, T], *args: A.args, **kwargs: A.kwargs
    ) -> T:
        """Исполнить sync-функцию в потоке браузера, к которому относится `target`."""
        return await self.thread_of(target).call(fn, *args, **kwargs)

    # --- процесс SDK -------------------------------------------------------------------

    async def prepare(self) -> None:
        """Подготовка — в служебном потоке."""
        if self._service is None:
            self._service = BrowserThread("browser-pool-driver")
        await self._service.run(self.driver.prepare)

    async def shutdown(self) -> None:
        """Освобождение — в служебном потоке; потом поток останавливается."""
        service, self._service = self._service, None
        if service is None:
            service = BrowserThread("browser-pool-driver")
        try:
            await service.run(self.driver.shutdown)
        finally:
            service.stop()

    # --- браузер -----------------------------------------------------------------------

    async def launch(self, spec: LaunchSpec) -> B:
        """Запуск — в новом потоке, который станет потоком этого браузера."""
        return await self._start(lambda: self.driver.launch(spec))

    async def attach(self, endpoint: Endpoint) -> B:
        """Подключение — в новом потоке, как запуск."""
        return await self._start(lambda: self.driver.attach(endpoint))

    async def ping(self, browser: B) -> bool:
        """Проверка живости — в потоке браузера."""
        return await self.thread_of(browser).run(lambda: self.driver.ping(browser))

    def on_disconnect(self, browser: B, callback: Callable[[], None]) -> None:
        """Колбэк отключения приходит в цикл пула, из какого бы потока его ни позвал SDK."""
        loop = asyncio.get_running_loop()

        def notify() -> None:
            with contextlib.suppress(RuntimeError):  # цикл пула уже закрыт
                loop.call_soon_threadsafe(callback)

        self.driver.on_disconnect(browser, notify)

    async def close_browser(self, browser: B) -> None:
        """Закрыть; удалось — поток браузера больше не нужен. Нет — он ждёт `kill_browser`."""
        await self.thread_of(browser).run(lambda: self.driver.close_browser(browser))
        self._forget_browser(browser)

    async def kill_browser(self, browser: B) -> None:
        """Добить; поток браузера останавливается в любом случае."""
        try:
            await self.thread_of(browser).run(lambda: self.driver.kill_browser(browser))
        finally:
            self._forget_browser(browser)

    def pid(self, browser: B) -> int | None:
        """PID, снятый в потоке браузера при запуске."""
        if id(browser) in self._pids:
            return self._pids[id(browser)]
        return self.driver.pid(browser)

    # --- контекст ----------------------------------------------------------------------

    async def new_context(self, browser: B, spec: ContextSpec) -> C:
        """Контекст — в потоке браузера; его вызовы потом идут туда же."""
        thread = self.thread_of(browser)
        context = await thread.run(lambda: self.driver.new_context(browser, spec))
        self._bind(context, thread)
        return context

    async def close_context(self, context: C) -> None:
        """Закрыть контекст в потоке браузера."""
        try:
            await self.thread_of(context).run(lambda: self.driver.close_context(context))
        finally:
            self._unbind(context)

    async def export_state(self, context: C) -> SessionState:
        """Снять состояние в потоке браузера."""
        return await self.thread_of(context).run(lambda: self.driver.export_state(context))

    async def add_cookies(self, context: C, cookies: Sequence[Cookie]) -> None:
        """Добавить куки в потоке браузера."""
        await self.thread_of(context).run(lambda: self.driver.add_cookies(context, cookies))

    # --- вкладка -----------------------------------------------------------------------

    async def new_page(self, context: C) -> P:
        """Вкладка — в потоке браузера; её вызовы потом идут туда же."""
        thread = self.thread_of(context)
        page = await thread.run(lambda: self.driver.new_page(context))
        self._bind(page, thread)
        return page

    def page_usable(self, page: P) -> bool:
        """Из потока пула: драйвер с `thread_affinity` отвечает по своему состоянию."""
        return self.driver.page_usable(page)

    async def close_page(self, page: P) -> None:
        """Закрыть вкладку в потоке браузера."""
        try:
            await self.thread_of(page).run(lambda: self.driver.close_page(page))
        finally:
            self._unbind(page)

    async def capture(self, page: P) -> Evidence:
        """Снимок вкладки в потоке браузера."""
        return await self.thread_of(page).run(lambda: self.driver.capture(page))

    def classify(self, error: BaseException) -> ErrorKind | None:
        """Классификация — чистая функция, поток не нужен."""
        return self.driver.classify(error)

    # --- внутреннее --------------------------------------------------------------------

    async def _start(self, factory: Callable[[], Coroutine[Any, Any, B]]) -> B:
        thread = BrowserThread(f"browser-pool-browser-{next(_names)}")

        async def started() -> tuple[B, int | None]:
            browser = await factory()
            return browser, self.driver.pid(browser)

        try:
            browser, pid = await thread.run(started)
        except BaseException:
            thread.stop()
            raise
        self._bind(browser, thread)
        self._pids[id(browser)] = pid
        return browser

    def _bind(self, target: object, thread: BrowserThread) -> None:
        self._threads[id(target)] = (target, thread)

    def _unbind(self, target: object) -> None:
        entry = self._threads.get(id(target))
        if entry is not None and entry[0] is target:
            del self._threads[id(target)]

    def _forget_browser(self, browser: B) -> None:
        """Браузера больше нет: забыть его и всё, что жило в его потоке, остановить поток."""
        entry = self._threads.get(id(browser))
        if entry is None or entry[0] is not browser:
            return
        thread = entry[1]
        for key, (_, owner) in tuple(self._threads.items()):
            if owner is thread:
                del self._threads[key]
        self._pids.pop(id(browser), None)
        thread.stop()


class _Windows[B, C, P](ThreadBoundDriver[B, C, P]):
    """Управление окнами исходного драйвера — в потоке браузера."""

    def _windows(self) -> WindowControl[B, P]:
        return cast("WindowControl[B, P]", self.driver)

    async def window_of(self, page: P) -> WindowId:
        """Окно вкладки."""
        return await self.thread_of(page).run(lambda: self._windows().window_of(page))

    async def get_bounds(self, browser: B, window: WindowId) -> WindowBounds:
        """Где окно."""
        return await self.thread_of(browser).run(
            lambda: self._windows().get_bounds(browser, window)
        )

    async def set_bounds(self, browser: B, window: WindowId, bounds: WindowBounds) -> None:
        """Поставить окно на место."""
        await self.thread_of(browser).run(
            lambda: self._windows().set_bounds(browser, window, bounds)
        )

    async def screen_area(self, page: P) -> Rect:
        """Рабочая область монитора."""
        return await self.thread_of(page).run(lambda: self._windows().screen_area(page))

    async def bring_to_front(self, page: P) -> None:
        """Поднять окно наверх."""
        await self.thread_of(page).run(lambda: self._windows().bring_to_front(page))


class _Labels[B, C, P](ThreadBoundDriver[B, C, P]):
    """Подписи окон исходного драйвера — в потоке браузера."""

    async def label_page(self, page: P, label: str) -> None:
        """Подписать окно вкладки."""
        labeler = cast("PageLabeler[P]", self.driver)
        await self.thread_of(page).run(lambda: labeler.label_page(page, label))


class _WindowsAndLabels[B, C, P](_Windows[B, C, P], _Labels[B, C, P]):
    """И окна, и подписи."""


def bind_threads[B, C, P](driver: Driver[B, C, P]) -> ThreadBoundDriver[B, C, P]:
    """Обернуть драйвер: браузер — свой поток. Необязательные части драйвера сохраняются."""
    windows = isinstance(driver, WindowControl)
    labels = isinstance(driver, PageLabeler)
    if windows and labels:
        return _WindowsAndLabels(driver)
    if windows:
        return _Windows(driver)
    if labels:
        return _Labels(driver)
    return ThreadBoundDriver(driver)


async def call_in_thread[**A, T](
    driver: object, target: object, fn: Callable[A, T], *args: A.args, **kwargs: A.kwargs
) -> T:
    """Sync-функция в потоке браузера `target`; у драйвера без потоков — прямо в цикле пула."""
    if isinstance(driver, ThreadBoundDriver):
        bound = cast("ThreadBoundDriver[Any, Any, Any]", driver)
        return await bound.call(target, fn, *args, **kwargs)
    return fn(*args, **kwargs)


__all__ = ["BrowserThread", "ThreadBoundDriver", "bind_threads", "call_in_thread"]
