"""Клиент почты на pydoll (API 3.x: `current_url()` и `text()` — методы)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from examples.accounts.mail_sdk.errors import (
    LoginFailedError,
    NotLoggedInError,
    WrongMailboxError,
)

if TYPE_CHECKING:
    from pydoll.browser.tab import Tab

_WAIT = 15


class PydollMailClient:
    """`MailClient` поверх вкладки pydoll."""

    def __init__(self, tab: Tab, *, base_url: str) -> None:
        """`base_url` — адрес сайта почты без завершающего `/`."""
        self.tab = tab
        self._base = base_url

    async def is_logged_in(self) -> bool:
        """Открыть ящик; `False` — сайт отправил на форму входа."""
        await self.tab.go_to(f"{self._base}/inbox", timeout=_WAIT)
        return not (await self.tab.current_url()).endswith("/login")

    async def login(self, login: str, password: str) -> None:
        """Войти паролем. Не принял — `LoginFailedError`."""
        await self.tab.go_to(f"{self._base}/login", timeout=_WAIT)
        await (await self.tab.query("#login")).insert_text(login)
        await (await self.tab.query("#password")).insert_text(password)
        await (await self.tab.query("#submit")).click()
        # После отправки формы страница переходит в ящик: ждём его заголовок.
        if await self.tab.query("#owner", timeout=_WAIT, raise_exc=False) is None:
            msg = f"вход {login} не принят"
            raise LoginFailedError(msg)

    async def check_inbox(self, login: str) -> None:
        """Ящик открыт и он `login`."""
        if not await self.is_logged_in():
            msg = f"{login}: сайт требует входа"
            raise NotLoggedInError(msg)
        owner = await (await self.tab.query("#owner", timeout=_WAIT)).text()
        if owner != login:
            msg = f"в ящике {owner}, а ждали {login}"
            raise WrongMailboxError(msg)
