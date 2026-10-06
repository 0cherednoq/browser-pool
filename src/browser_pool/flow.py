"""Сессия identity глазами site SDK: единственная точка его встречи с пулом.

Site SDK знает сайт — как войти, как понять, что сессия жива, как подготовить вкладку. Пул знает
всё остальное: где открыть контекст, с каким прокси, что восстановить, когда сохранить. Их стык —
`SessionFlow`:

    class MailFlow(BaseFlow[BrowserContext, Page, MailSession]):
        async def open(self, ctx):
            page = await ctx.new_page()  # временная — пул закроет её сам
            if ctx.restored is None or not await mail.session_alive(page):
                await mail.login(page, ctx.identity.payload)
            return MailSession(mail_url=page.url)

        async def prepare_page(self, session, page):
            await mail.open_mailbox(page, session.mail_url)  # SPA грузится один раз

Сохранять состояние после входа flow не обязан: пул сохраняет его сразу после `open`.
Без flow пул работает как пул анонимных контекстов.
"""

# Цикл `flow` ↔ `identity` только в аннотациях (под TYPE_CHECKING): identity несёт свой flow,
# а flow получает identity. В рантайме модули не импортируют друг друга.
# pyright: reportImportCycles=false
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

    from browser_pool.identity import Identity
    from browser_pool.proxies import Proxy
    from browser_pool.state import SessionState


class OpenRequest[C, P](Protocol):
    """Что пул даёт flow при открытии сессии."""

    @property
    def identity(self) -> Identity:
        """Чья сессия; `identity.payload` — креды и модель аккаунта."""
        ...

    @property
    def context(self) -> C:
        """Нативный контекст SDK, в котором открывается сессия."""
        ...

    @property
    def restored(self) -> SessionState | None:
        """Что пул восстановил в контекст: сохранённое или `StatePolicy.initial`. `None` — ничего."""
        ...

    @property
    def proxy(self) -> Proxy | None:
        """Прокси контекста, если он есть."""
        ...

    async def new_page(self) -> P:
        """Временная вкладка для входа; пул закроет её после `open`, что бы ни случилось."""
        ...

    async def save_state(self) -> None:
        """Сохранить состояние прямо сейчас — например, между шагами долгого входа."""
        ...

    async def call[**A, T](self, fn: Callable[A, T], /, *args: A.args, **kwargs: A.kwargs) -> T:
        """Исполнить sync-код входа в потоке браузера (`thread_affinity`: Selenium).

        У асинхронных драйверов потока браузера нет — `fn` исполняется прямо здесь.
        """
        ...


@runtime_checkable
class SessionFlow[C, P, S](Protocol):
    """Как открыть сессию identity на сайте и подготовить к работе её вкладки."""

    async def open(self, ctx: OpenRequest[C, P]) -> S:
        """Открыть сессию: восстановленная жива — принять, нет — войти. Вернуть объект сессии."""
        ...

    async def prepare_page(self, session: S, page: P) -> None:
        """Прогрев новой вкладки — один раз, дальше вкладка тёплая."""
        ...

    async def reset_page(self, session: S, page: P) -> bool:
        """Перед возвратом вкладки в запас. `False` — вкладку закрыть, её состояние испорчено."""
        ...

    async def close(self, session: S) -> None:
        """Сессия закрывается вместе с контекстом."""
        ...


class BaseFlow[C, P, S](ABC):
    """`SessionFlow` с разумным поведением по умолчанию: обязателен только `open`."""

    @abstractmethod
    async def open(self, ctx: OpenRequest[C, P]) -> S:
        """Открыть сессию identity."""

    async def prepare_page(self, session: S, page: P) -> None:
        """По умолчанию вкладке прогрев не нужен."""
        _ = session, page

    async def reset_page(self, session: S, page: P) -> bool:
        """По умолчанию вкладка возвращается в запас как есть."""
        _ = session, page
        return True

    async def close(self, session: S) -> None:
        """По умолчанию закрывать нечего."""
        _ = session


__all__ = ["BaseFlow", "OpenRequest", "SessionFlow"]
