"""Аренды: единственный способ владеть ресурсом пула.

Арендатор получает нативные объекты SDK — вкладку, контекст, браузер — без обёрток: site SDK
пишется под свой SDK и про пул не знает. Аренда живёт внутри `async with`; после выхода её
объекты трогать нельзя — они уже отданы следующему.

- `PageLease` (`pool.page`) — вкладка в контексте identity; контекст делят несколько аренд.
- `ContextLease` (`pool.context`) — контекст identity целиком и эксклюзивно, для сценариев, где
  site SDK сам управляет вкладками. Открытые им вкладки он и закрывает.

Исход аренды решается при выходе: штатно — вкладка возвращается тёплой; исключение или
`discard_page()` — вкладка закрывается (её состояние неизвестно); `retire_context()` — контекст identity
выводится из работы и закроется, когда вернут все его аренды; `report(kind)` — арендатор сам
говорит, что сломалось, и это важнее любой классификации исключения.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Protocol, cast

from browser_pool.errors import Classification, ErrorKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from browser_pool.identity import Identity
    from browser_pool.proxies import Proxy
    from browser_pool.state import Cookie


class LeaseControl(Protocol):
    """Внутреннее: что аренда просит у пула. Пользователю пула не нужен."""

    def attachments_of(self, lease_id: int) -> dict[str, object]:
        """Вложения вкладки аренды."""
        ...

    async def retire_context(self, key: str, *, generation: int, reason: str) -> None:
        """Вывести контекст identity этого поколения из работы."""
        ...

    async def cookies_of(self, lease_id: int, *, domain: str | None) -> tuple[Cookie, ...]:
        """Куки контекста аренды."""
        ...

    async def call_in[**A, T](
        self, target: object, fn: Callable[A, T], /, *args: A.args, **kwargs: A.kwargs
    ) -> T:
        """Исполнить sync-функцию в потоке браузера, к которому относится `target`."""
        ...


class ContextLease[B, C, S]:
    """Арендованный контекст identity и всё, что к нему относится."""

    __slots__ = (
        "_committed",
        "_control",
        "_reported",
        "browser",
        "browser_id",
        "context",
        "generation",
        "identity",
        "lease_id",
        "proxy",
        "session",
    )

    def __init__(
        self,
        *,
        control: LeaseControl,
        lease_id: int,
        identity: Identity,
        browser_id: str,
        generation: int,
        browser: B,
        context: C,
        session: S,
        proxy: Proxy | None = None,
    ) -> None:
        self._control = control
        self._reported: Classification | None = None
        self._committed = False
        self.lease_id: int = lease_id
        """Сквозной ключ аренды — в событиях и логах."""
        self.identity: Identity = identity
        """Identity, которой досталась аренда (для `any_of` — одна из кандидатов)."""
        self.browser_id: str = browser_id
        """Браузер пула, в котором открыт контекст."""
        self.generation: int = generation
        """Поколение контекста identity."""
        self.browser: B = browser
        """Нативный браузер SDK."""
        self.context: C = context
        """Нативный контекст SDK."""
        self.session: S = session
        """Объект сессии, который вернул `SessionFlow.open`; `None` — flow нет."""
        self.proxy: Proxy | None = proxy
        """Прокси контекста: `proxy.url` — для HTTP-клиента той же сессии. `None` — напрямую."""

    @property
    def reported(self) -> Classification | None:
        """Что арендатор сообщил о сбое через `report`, если сообщал."""
        return self._reported

    @property
    def committed(self) -> bool:
        """Арендатор объявил точку невозврата (`commit`): результат мог быть применён."""
        return self._committed

    def commit(self) -> None:
        """Точка невозврата: необратимое действие могло совершиться — `pool.run` эту попытку не повторит.

        Зовите сразу перед отправкой письма, платежа, публикации и подобного. Если после этого
        попытка упадёт, исключение уйдёт наружу как есть, даже когда вид сбоя повторяемый. Пул
        при этом реагирует на ресурсы как обычно: вкладка выбрасывается, браузер уходит в карантин.
        """
        self._committed = True

    def report(self, kind: ErrorKind | str, *, retry_after: float | None = None) -> None:
        """Сказать пулу, что сломалось, — пул отреагирует при выходе из аренды.

        Важнее классификации исключения: если после `report` вылетит исключение-следствие,
        реакция будет по сообщённому виду. `retry_after` — только для `rate_limited`.
        """
        self._reported = Classification(ErrorKind(kind), retry_after)

    async def retire_context(self, reason: str = "") -> None:
        """Вывести контекст identity из работы: новых аренд он не получит и закроется после текущих."""
        await self._control.retire_context(
            self.identity.key, generation=self.generation, reason=reason
        )

    async def cookies(self, *, domain: str | None = None) -> tuple[Cookie, ...]:
        """Куки контекста сейчас — для HTTP-клиента (`curl_cffi`, `httpx`) той же сессии.

        `domain` — хост запроса: только куки, которые браузер отправил бы на него. Вместе с
        `proxy.url` клиент идёт тем же адресом и с той же сессией, что и браузер.
        """
        return await self._control.cookies_of(self.lease_id, domain=domain)

    async def call[**A, T](self, fn: Callable[A, T], /, *args: A.args, **kwargs: A.kwargs) -> T:
        """Исполнить sync-код site SDK в потоке браузера аренды и дождаться результата.

        Для синхронных SDK (`thread_affinity`: Selenium): все вызовы одного браузера идут из
        его потока, и `fn` — тоже, цикл пула при этом не блокируется. У асинхронных драйверов
        потока браузера нет — `fn` исполняется прямо здесь.
        """
        return await self._control.call_in(self.browser, fn, *args, **kwargs)


class PageLease[B, C, P, S](ContextLease[B, C, S]):
    """Арендованная вкладка и всё, что к ней относится."""

    __slots__ = ("_discard", "page")

    def __init__(
        self,
        *,
        control: LeaseControl,
        lease_id: int,
        identity: Identity,
        browser_id: str,
        generation: int,
        browser: B,
        context: C,
        page: P,
        session: S,
        proxy: Proxy | None = None,
    ) -> None:
        super().__init__(
            control=control,
            lease_id=lease_id,
            identity=identity,
            browser_id=browser_id,
            generation=generation,
            browser=browser,
            context=context,
            session=session,
            proxy=proxy,
        )
        self._discard = False
        self.page: P = page
        """Нативная вкладка SDK."""

    @property
    def discarding(self) -> bool:
        """Вкладка будет закрыта при возврате, а не отдана тёплой."""
        return self._discard

    def discard_page(self) -> None:
        """Не возвращать вкладку в запас: её состояние испорчено. Закроется при выходе из аренды."""
        self._discard = True

    async def attachment[T](self, key: str, factory: Callable[[P], T | Awaitable[T]]) -> T:
        """Объект site SDK, привязанный к вкладке: создаётся один раз и живёт, пока жива вкладка.

        Так обёртка над страницей, которая подписывается на её события в конструкторе, не
        плодит подписки на каждую аренду тёплой вкладки. При закрытии вкладки пул вызывает у
        вложения `aclose()` или `close()`, если они есть.
        """
        attachments = self._control.attachments_of(self.lease_id)
        if key not in attachments:
            created = factory(self.page)
            attachments[key] = await created if inspect.isawaitable(created) else created
        return cast("T", attachments[key])


__all__ = ["ContextLease", "PageLease"]
