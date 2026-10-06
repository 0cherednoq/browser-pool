"""Flow — тонкий адаптер: открыть сессию identity вызовами SDK почты. Один на оба SDK браузера."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, override

from browser_pool import BaseFlow, OpenRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from examples.accounts.mail_sdk import MailClient

type ClientFactory = Callable[[Any], MailClient]
"""Клиент почты на нативной вкладке SDK браузера: `PlaywrightMailClient(page, base_url=…)`."""


@dataclass(frozen=True, slots=True)
class Account:
    """Аккаунт приложения — payload identity. Пароль не попадает в repr и логи пула."""

    login: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MailSession:
    """Сессия аккаунта в контексте."""

    login: str


class MailFlow(BaseFlow[Any, Any, MailSession]):
    """Сессия жива — принять; нет — войти паролем. Проверка ящика — на каждой новой вкладке."""

    def __init__(self, client: ClientFactory) -> None:
        self.client = client

    @override
    async def open(self, ctx: OpenRequest[Any, Any]) -> MailSession:
        account: Account = ctx.identity.payload
        client = self.client(await ctx.new_page())  # временная вкладка: пул закроет её сам
        if not await client.is_logged_in():
            await client.login(account.login, account.password)
        await client.check_inbox(account.login)
        return MailSession(login=account.login)

    @override
    async def prepare_page(self, session: MailSession, page: Any) -> None:
        await self.client(page).check_inbox(session.login)
