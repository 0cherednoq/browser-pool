"""Ошибки SDK → виды сбоя пула. Одна таблица, на слое сборки: SDK о пуле не знает."""

from __future__ import annotations

from browser_pool import ErrorKind
from examples.quotes.quotes_sdk import LoginFailedError, SessionExpiredError, SiteChangedError


def classify(error: BaseException) -> ErrorKind | None:
    """Классификатор пула (`BrowserPool(classifier=…)`).

    - вход не принят — дело аккаунта: identity блокируется до `pool.unblock`, повторов нет;
    - вход потерян — контекст выводится, следующая аренда откроет сессию заново (`pool.run`
      повторит задачу);
    - разметка сайта не та — повтор не поможет: вкладка выбрасывается, ошибка уходит наверх.

    Чужие ошибки — `None`: их разберёт драйвер (упавший браузер, прокси, закрытая вкладка).
    """
    match error:
        case LoginFailedError():
            return ErrorKind.blocked
        case SessionExpiredError():
            return ErrorKind.session
        case SiteChangedError():
            return ErrorKind.unknown
        case _:
            return None
