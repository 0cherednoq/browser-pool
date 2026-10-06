"""SDK почты — слой 1: вход, проверка ящика. Две реализации одного интерфейса — на Playwright и pydoll.

О пуле браузеров SDK не знает и `browser_pool` не импортирует: получает вкладку своего SDK
браузера и адрес сайта. Реализация выбирается импортом (`mail_sdk.playwright` или
`mail_sdk.pydoll`) — каждая тянет только свой SDK браузера.
"""

from __future__ import annotations

from examples.accounts.mail_sdk.base import MailClient
from examples.accounts.mail_sdk.errors import (
    LoginFailedError,
    MailError,
    NotLoggedInError,
    WrongMailboxError,
)

__all__ = ["LoginFailedError", "MailClient", "MailError", "NotLoggedInError", "WrongMailboxError"]
