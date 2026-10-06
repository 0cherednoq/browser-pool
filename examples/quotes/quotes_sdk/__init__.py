"""SDK сайта quotes.toscrape.com на Playwright — слой 1.

Всё, что известно о сайте: адреса, селекторы, признаки входа, ошибки сайта. О пуле браузеров
SDK не знает и `browser_pool` не импортирует: он получает вкладку Playwright и работает с ней.
Такой SDK можно выложить отдельной библиотекой и использовать без пула — в скрипте, в тестах.

quotes.toscrape.com — учебный сайт для автоматизации: вход принимает любой логин и пароль,
вошедший видит ссылку «Logout». Этого хватает, чтобы показать настоящий вход, сессию в куках и её
восстановление без повторного ввода пароля.
"""

from __future__ import annotations

from examples.quotes.quotes_sdk.client import BASE, QuotesClient
from examples.quotes.quotes_sdk.errors import (
    LoginFailedError,
    QuotesError,
    SessionExpiredError,
    SiteChangedError,
)
from examples.quotes.quotes_sdk.models import Quote, QuotesPage
from examples.quotes.quotes_sdk.vault import SessionVault

__all__ = [
    "BASE",
    "LoginFailedError",
    "Quote",
    "QuotesClient",
    "QuotesError",
    "QuotesPage",
    "SessionExpiredError",
    "SessionVault",
    "SiteChangedError",
]
