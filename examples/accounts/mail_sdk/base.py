"""Интерфейс клиента почты — общий для реализаций на разных SDK браузера."""

from __future__ import annotations

from typing import Protocol


class MailClient(Protocol):
    """Операции почты на одной вкладке. Вкладку создаёт и закрывает вызывающий."""

    async def is_logged_in(self) -> bool:
        """Открыть ящик; `False` — сайт отправил на форму входа."""
        ...

    async def login(self, login: str, password: str) -> None:
        """Войти паролем. Не принял — `LoginFailedError`."""
        ...

    async def check_inbox(self, login: str) -> None:
        """Ящик открыт и он `login`. Не вошли — `NotLoggedInError`; чужой — `WrongMailboxError`."""
        ...
