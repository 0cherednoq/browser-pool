"""Ошибки SDK почты → виды сбоя пула. Одна таблица, на слое сборки: SDK о пуле не знает."""

from __future__ import annotations

from browser_pool import ErrorKind
from examples.accounts.mail_sdk import LoginFailedError, NotLoggedInError, WrongMailboxError


def classify(error: BaseException) -> ErrorKind | None:
    """Классификатор пула (`BrowserPool(classifier=…)`).

    - пароль не подошёл — дело аккаунта: identity блокируется, повторов нет;
    - сессии нет или в контексте чужая — контекст выводится, следующая аренда войдёт заново.

    Чужие ошибки — `None`: их разберёт драйвер (упавший браузер, прокси, закрытая вкладка).
    """
    match error:
        case LoginFailedError():
            return ErrorKind.blocked
        case NotLoggedInError() | WrongMailboxError():
            return ErrorKind.session
        case _:
            return None
