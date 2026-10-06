"""Тёплые вкладки одного контекста: рабочее место арендатора, а не одноразовый ресурс.

Вкладка живёт дольше аренды: SPA сайта грузится один раз при прогреве (`prepare`), и
следующая аренда той же identity получает уже готовую вкладку. Сколько вкладок identity может
занимать одновременно, решает планировщик; здесь — откуда вкладку взять и что сделать с ней
при возврате.

- Берётся самая свежая тёплая (LIFO): лишние тогда простаивают и закрываются по старости.
- Возвращённая вкладка проходит сброс (`reset`); не прошла или умерла — закрывается, её
  состояние неизвестно, дешевле открыть новую.
- Тёплых больше `warm_limit` — закрывается самая холодная.

Каждая операция ограничена своим таймаутом. Закрытие не бросает никогда: оно идёт и после
ошибки арендатора, и заслонять её не должно. Незакрывшаяся вкладка всё равно перестаёт
числиться за пулом, а сбой учитывается в `close_failures` — по нему контекст выводят из работы.

Вложения (`attachments`) — объекты site SDK, привязанные к вкладке: живут, пока жива она, и
закрываются (`aclose` или `close`) раньше неё — им может понадобиться живая страница.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from typing import TYPE_CHECKING, Any, NamedTuple

from browser_pool.clock import monotonic

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from browser_pool.config import Timeouts
    from browser_pool.driver import Driver

_logger = logging.getLogger(__name__)


class _Idle[P](NamedTuple):
    page: P
    since: float


class TabPool[C, P]:
    """Вкладки одного контекста: тёплые в запасе и счёт открытых."""

    def __init__(
        self,
        driver: Driver[Any, C, P],
        context: C,
        *,
        prepare: Callable[[P], Awaitable[None]] | None,
        reset: Callable[[P], Awaitable[bool]] | None,
        warm_limit: int,
        timeouts: Timeouts,
    ) -> None:
        self._driver = driver
        self._context = context
        self._prepare = prepare
        self._reset = reset
        self._warm_limit = warm_limit
        self._timeouts = timeouts
        # Справа — самые свежие: берём справа, старые закрываем слева.
        self._idle: deque[_Idle[P]] = deque()
        self._open = 0
        self._attachments: dict[int, dict[str, object]] = {}
        self.close_failures: int = 0
        """Сколько вкладок не закрылось штатно. Растёт — контекст пора выводить из работы."""

    @property
    def open(self) -> int:
        """Сколько вкладок открыто: занятые и тёплые."""
        return self._open

    @property
    def idle(self) -> int:
        """Сколько тёплых вкладок простаивает."""
        return len(self._idle)

    @property
    def coldest_idle_since(self) -> float | None:
        """С какого момента простаивает самая холодная тёплая вкладка; `None` — тёплых нет."""
        return self._idle[0].since if self._idle else None

    async def take(self) -> P:
        """Вкладка для аренды: тёплая, если есть живая, иначе новая и прогретая."""
        while self._idle:
            page = self._idle.pop().page
            if self._driver.page_usable(page):
                return page
            await self.discard(page)
        return await self._open_page()

    async def give_back(self, page: P) -> None:
        """Вернуть вкладку после аренды: в запас, если она цела и прошла сброс, иначе закрыть."""
        try:
            reusable = await self._reusable(page)
        except BaseException:
            await self.discard(page)  # отмена посреди сброса: состояние вкладки неизвестно
            raise
        if not reusable:
            await self.discard(page)
            return
        self._idle.append(_Idle(page, monotonic()))
        while len(self._idle) > self._warm_limit:
            await self.discard(self._idle.popleft().page)

    def attachments(self, page: P) -> dict[str, object]:
        """Вложения вкладки: объекты site SDK, которые живут и закрываются вместе с ней."""
        return self._attachments.setdefault(id(page), {})

    async def discard(self, page: P) -> None:
        """Закрыть вкладку и забыть о ней. Не бросает: сбой закрытия только учитывается."""
        self._open -= 1
        await self._close_attachments(id(page))
        try:
            async with asyncio.timeout(self._timeouts.close):
                await self._driver.close_page(page)
        except Exception:
            self.close_failures += 1
            _logger.warning("Вкладка не закрылась штатно", exc_info=True)

    async def trim_idle(self) -> bool:
        """Закрыть самую холодную тёплую вкладку — освободить место соседу. `False` — нечего."""
        if not self._idle:
            return False
        await self.discard(self._idle.popleft().page)
        return True

    async def expire_idle(self, *, idle_ttl: float) -> int:
        """Закрыть вкладки, простаивающие дольше `idle_ttl` секунд. Возвращает, сколько закрыто."""
        deadline = monotonic() - idle_ttl
        closed = 0
        while self._idle and self._idle[0].since <= deadline:
            await self.discard(self._idle.popleft().page)
            closed += 1
        return closed

    async def aclose(self) -> None:
        """Закрыть тёплые вкладки и вложения занятых: занятые вкладки умрут вместе с контекстом."""
        while self._idle:
            await self.discard(self._idle.popleft().page)
        for key in tuple(self._attachments):
            await self._close_attachments(key)

    async def _close_attachments(self, key: int) -> None:
        for attachment in self._attachments.pop(key, {}).values():
            try:
                async with asyncio.timeout(self._timeouts.close):
                    await _close_attachment(attachment)
            except Exception:
                _logger.warning("Вложение вкладки не закрылось", exc_info=True)

    async def _open_page(self) -> P:
        async with asyncio.timeout(self._timeouts.page_create):
            page = await self._driver.new_page(self._context)
        self._open += 1
        if self._prepare is None:
            return page
        try:
            async with asyncio.timeout(self._timeouts.prepare_page):
                await self._prepare(page)
        except BaseException:
            await self.discard(page)
            raise
        return page

    async def _reusable(self, page: P) -> bool:
        if not self._driver.page_usable(page):
            return False
        if self._reset is None:
            return True
        try:
            async with asyncio.timeout(self._timeouts.reset_page):
                return await self._reset(page)
        except Exception:
            _logger.warning("Сброс вкладки перед возвратом не удался", exc_info=True)
            return False


async def _close_attachment(attachment: object) -> None:
    """`aclose()` или `close()` вложения — что есть; синхронный `close` тоже годится."""
    closer = getattr(attachment, "aclose", None) or getattr(attachment, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


__all__ = ["TabPool"]
