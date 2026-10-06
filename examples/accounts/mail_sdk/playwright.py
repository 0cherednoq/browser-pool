"""Клиент почты на Playwright."""

from __future__ import annotations

from typing import TYPE_CHECKING

from examples.accounts.mail_sdk.errors import (
    LoginFailedError,
    NotLoggedInError,
    WrongMailboxError,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


class PlaywrightMailClient:
    """`MailClient` поверх вкладки Playwright."""

    def __init__(self, page: Page, *, base_url: str) -> None:
        """`base_url` — адрес сайта почты без завершающего `/`."""
        self.page = page
        self._base = base_url

    async def is_logged_in(self) -> bool:
        """Открыть ящик; `False` — сайт отправил на форму входа."""
        await self.page.goto(f"{self._base}/inbox")
        return not self.page.url.endswith("/login")

    async def login(self, login: str, password: str) -> None:
        """Войти паролем. Не принял — `LoginFailedError`."""
        await self.page.goto(f"{self._base}/login")
        await self.page.fill("#login", login)
        await self.page.fill("#password", password)
        async with self.page.expect_navigation():
            await self.page.click("#submit")
        if not await self.page.locator("#owner").count():
            msg = f"вход {login} не принят"
            raise LoginFailedError(msg)

    async def check_inbox(self, login: str) -> None:
        """Ящик открыт и он `login`."""
        if not await self.is_logged_in():
            msg = f"{login}: сайт требует входа"
            raise NotLoggedInError(msg)
        owner = await self.page.inner_text("#owner")
        if owner != login:
            msg = f"в ящике {owner}, а ждали {login}"
            raise WrongMailboxError(msg)
