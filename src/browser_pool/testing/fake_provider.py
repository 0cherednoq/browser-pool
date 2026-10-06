"""Фейковый провайдер эндпоинтов: браузеры «от вендора» для `FakeDriver` — без вендора.

Выдаёт эндпоинты, к которым подключается `FakeDriver.attach`, и ведёт учёт, по которому видно,
выполняет ли пул обещание провайдерам: каждый выданный эндпоинт освобождён ровно один
раз. Сверх этого — заказ сбоев `start`/`stop` и сироты «прошлого запуска» для `reap_orphans`.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING

from browser_pool.driver import Endpoint

if TYPE_CHECKING:
    from browser_pool.provider import EndpointRequest


class FakeEndpointProvider:
    """Провайдер без браузеров. Реализует `EndpointProvider`."""

    def __init__(self, *, orphans: int = 0) -> None:
        """`orphans` — сколько сирот прошлого запуска найдёт первый `reap_orphans`."""
        self.requests: list[EndpointRequest] = []
        """Все заявки `start` — в порядке вызова, включая упавшие."""
        self.stopped: list[Endpoint] = []
        """Все вызовы `stop` — в порядке вызова, включая повторные и упавшие."""
        self.orphans: int = orphans
        self.start_error: BaseException | None = None
        """Следующий `start` упадёт с этой ошибкой (один раз)."""
        self.stop_error: BaseException | None = None
        """Следующий `stop` упадёт с этой ошибкой (один раз) — эндпоинт при этом освобождён."""
        self.start_delay: float = 0.0
        """Сколько секунд идёт каждый `start` — для проверок таймаутов."""
        self._active: dict[str, Endpoint] = {}
        self._issued: set[str] = set()
        self._ids = itertools.count(1)

    @property
    def active(self) -> tuple[Endpoint, ...]:
        """Выданные и не освобождённые эндпоинты. После остановки пула — пусто."""
        return tuple(self._active.values())

    @property
    def double_stops(self) -> int:
        """Сколько раз эндпоинт освобождали повторно или не выдав. Пул обещает ноль."""
        urls = [endpoint.url for endpoint in self.stopped]
        return len(urls) - len(set(urls)) + len(set(urls) - self._issued)

    async def start(self, request: EndpointRequest) -> Endpoint:
        """Выдать эндпоинт для заявки."""
        self.requests.append(request)
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        error, self.start_error = self.start_error, None
        if error is not None:
            raise error
        endpoint = Endpoint(kind="cdp", url=f"fake://endpoint/{next(self._ids)}")
        self._active[endpoint.url] = endpoint
        self._issued.add(endpoint.url)
        return endpoint

    async def stop(self, endpoint: Endpoint) -> None:
        """Освободить эндпоинт."""
        await asyncio.sleep(0)
        self.stopped.append(endpoint)
        self._active.pop(endpoint.url, None)
        error, self.stop_error = self.stop_error, None
        if error is not None:
            raise error

    async def reap_orphans(self) -> int:
        """Сироты прошлого запуска — один раз."""
        await asyncio.sleep(0)
        found, self.orphans = self.orphans, 0
        return found


__all__ = ["FakeEndpointProvider"]
