"""Flow — тонкий адаптер: открыть сессию identity вызовами SDK. Знание о сайте — только в SDK."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

from browser_pool import BaseFlow, OpenRequest
from examples.quotes.quotes_sdk import QuotesClient

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

    from examples.quotes.quotes_sdk import SessionVault


@dataclass(frozen=True, slots=True)
class Account:
    """Аккаунт приложения — payload identity. Пароль не попадает в repr и логи пула."""

    login: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class QuotesSession:
    """Сессия аккаунта в контексте: чья и откуда взялась."""

    login: str
    restored: bool
    """`True` — сохранённая сессия оказалась живой, пароль не вводился."""


class QuotesFlow(BaseFlow["BrowserContext", "Page", QuotesSession]):
    """Сохранённая сессия жива — принять её; нет — войти паролем.

    Кто хранит сессию — выбирает приложение: пул (`vault=None`, состояние в `StateStore`) или
    SDK (`vault` задан, у identity `StatePolicy(mode="none")`).
    """

    def __init__(self, *, vault: SessionVault | None = None) -> None:
        self.vault = vault
        self.password_logins: Counter[str] = Counter()
        """Сколько раз каждый аккаунт вводил пароль — приложение проверяет, что по разу."""

    @override
    async def open(self, ctx: OpenRequest[BrowserContext, Page]) -> QuotesSession:
        account: Account = ctx.identity.payload
        if self.vault is not None:
            await self.vault.restore(ctx.context, account.login)
        client = QuotesClient(await ctx.new_page())  # временная вкладка: пул закроет её сам
        if await client.is_logged_in():
            return QuotesSession(login=account.login, restored=True)
        await client.login(account.login, account.password)
        self.password_logins[account.login] += 1
        if self.vault is not None:
            await self.vault.save(ctx.context, account.login)
        else:
            await ctx.save_state()  # сразу: падение процесса не заставит входить снова
        return QuotesSession(login=account.login, restored=False)
