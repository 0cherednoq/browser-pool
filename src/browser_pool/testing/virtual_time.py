"""Цикл событий с виртуальным временем: TTL в десять минут проверяется за миллисекунды.

`loop.time()` возвращает виртуальное время. Когда в цикле не осталось готовых задач и ждать
можно только таймеров, время прыгает сразу к ближайшему таймеру. Поэтому `asyncio.sleep`,
`asyncio.timeout`, `call_later` — и всё, что пул строит на `browser_pool.clock`, — идут без
реального ожидания и в детерминированном порядке.

Настоящий ввод-вывод ждётся по-настоящему: сокеты и колбэки из других потоков
(`call_soon_threadsafe`) приходят, как обычно. Пока в исполнителе цикла идёт работа
(`run_in_executor`, `asyncio.to_thread`, остановка исполнителя) или к циклу подключён настоящий
дескриптор (сокет, канал подпроцесса), время не прыгает: цикл ждёт по-настоящему, иначе таймер
(`asyncio.timeout` вокруг сетевого чтения) сработал бы раньше, чем ответил собеседник. Время тогда
идёт как настоящее; прыжки возвращаются, когда настоящего ввода-вывода не осталось.
"""

from __future__ import annotations

import asyncio
import selectors
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from concurrent.futures import Executor
    from selectors import SelectorKey

    from _typeshed import FileDescriptorLike


class _JumpingSelector(selectors.BaseSelector):
    """Селектор, который вместо ожидания таймера двигает виртуальное время цикла."""

    def __init__(self) -> None:
        self._real = selectors.DefaultSelector()
        self.now = 0.0
        self.real_work = 0
        """Сколько работ идёт в потоках: пока они есть, время не прыгает."""
        self.internal: frozenset[int] = frozenset()
        """Дескрипторы самого цикла (self-pipe): они не настоящий ввод-вывод."""

    @override
    def register(
        self, fileobj: FileDescriptorLike, events: int, data: object = None
    ) -> SelectorKey:
        return self._real.register(fileobj, events, data)

    @override
    def unregister(self, fileobj: FileDescriptorLike) -> SelectorKey:
        return self._real.unregister(fileobj)

    @override
    def modify(self, fileobj: FileDescriptorLike, events: int, data: object = None) -> SelectorKey:
        return self._real.modify(fileobj, events, data)

    @override
    def select(self, timeout: float | None = None) -> list[tuple[SelectorKey, int]]:
        ready = self._real.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            # Таймеров нет: ждать можно только настоящего I/O — ждём его по-настоящему.
            return self._real.select(None)
        if self.real_work or self._has_real_io():
            # Поток работает или к циклу подключён настоящий дескриптор: ждать по-настоящему.
            # Ответят — цикл проснётся; не ответят за весь срок таймера — срок и правда прошёл.
            ready = self._real.select(timeout)
            if not ready:
                self.now += timeout
            return ready
        self.now += timeout
        return []

    def _has_real_io(self) -> bool:
        return any(key.fd not in self.internal for key in self._real.get_map().values())

    @override
    def close(self) -> None:
        self._real.close()

    @override
    def get_map(self) -> Mapping[FileDescriptorLike, SelectorKey]:
        return self._real.get_map()


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """Цикл событий, в котором время идёт только тогда, когда ждать больше нечего."""

    def __init__(self, *, start: float = 0.0) -> None:
        self._jumping = _JumpingSelector()
        self._jumping.now = start
        super().__init__(self._jumping)
        # Сразу после создания зарегистрирован только self-pipe самого цикла.
        self._jumping.internal = frozenset(key.fd for key in self._jumping.get_map().values())

    @override
    def time(self) -> float:
        """Виртуальное время цикла, секунды."""
        return self._jumping.now

    @override
    def run_in_executor[T](
        self, executor: Executor | None, func: Callable[..., T], *args: Any
    ) -> asyncio.Future[T]:
        """Как обычно, но пока работа идёт в потоке, время не прыгает."""
        future = super().run_in_executor(executor, func, *args)
        self._jumping.real_work += 1
        future.add_done_callback(self._work_done)
        return future

    @override
    async def shutdown_default_executor(self, timeout: float | None = None) -> None:
        """Остановка исполнителя ждёт его потоки по-настоящему."""
        self._jumping.real_work += 1
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self._jumping.real_work -= 1

    def _work_done(self, _future: asyncio.Future[Any]) -> None:
        self._jumping.real_work -= 1


__all__ = ["VirtualTimeLoop"]
