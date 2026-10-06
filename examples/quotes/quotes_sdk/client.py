"""Клиент сайта поверх вкладки Playwright: вход, проверка входа, чтение страниц."""

from __future__ import annotations

from typing import TYPE_CHECKING

from examples.quotes.quotes_sdk.errors import (
    LoginFailedError,
    SessionExpiredError,
    SiteChangedError,
)
from examples.quotes.quotes_sdk.models import Quote, QuotesPage

if TYPE_CHECKING:
    from playwright.async_api import Page

BASE = "https://quotes.toscrape.com"
_LOGOUT = "a[href='/logout']"
_TIMEOUT = 20_000


class QuotesClient:
    """Операции сайта на одной вкладке. Вкладку создаёт и закрывает вызывающий."""

    def __init__(self, page: Page, *, base: str = BASE, timeout: float = _TIMEOUT) -> None:
        """`timeout` — миллисекунды на каждую навигацию и ожидание, как принято в Playwright."""
        self.page = page
        self._base = base
        self._timeout = timeout

    async def is_logged_in(self) -> bool:
        """Вошёл ли пользователь: главная страница показывает «Logout»."""
        await self.page.goto(f"{self._base}/", timeout=self._timeout)
        return await self._logout_visible()

    async def login(self, username: str, password: str) -> None:
        """Войти через форму. Не принял — `LoginFailedError`."""
        await self.page.goto(f"{self._base}/login", timeout=self._timeout)
        await self.page.fill("#username", username)
        await self.page.fill("#password", password)
        async with self.page.expect_navigation(timeout=self._timeout):
            # Enter в поле пароля, а не клик по кнопке: у видимого окна размер может меняться прямо
            # во время входа, а клик по координатам ждёт неподвижности кнопки.
            await self.page.press("#password", "Enter", timeout=self._timeout)
        if not await self._logout_visible():
            msg = f"сайт не принял вход {username}"
            raise LoginFailedError(msg)

    async def read_page(self, number: int) -> QuotesPage:
        """Страница цитат; только для вошедшего — иначе `SessionExpiredError`."""
        await self.page.goto(f"{self._base}/page/{number}/", timeout=self._timeout)
        if not await self._logout_visible():
            msg = f"страница {number}: вход потерян"
            raise SessionExpiredError(msg)
        texts = await self.page.locator(".quote .text").all_inner_texts()
        authors = await self.page.locator(".quote .author").all_inner_texts()
        if not texts or len(texts) != len(authors):
            msg = f"страница {number}: цитат {len(texts)}, авторов {len(authors)}"
            raise SiteChangedError(msg)
        return QuotesPage(
            number=number,
            quotes=tuple(
                Quote(text=text, author=author) for text, author in zip(texts, authors, strict=True)
            ),
        )

    async def _logout_visible(self) -> bool:
        return bool(await self.page.locator(_LOGOUT).count())
