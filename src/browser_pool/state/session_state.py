"""Состояние сессии: то, что делает браузер «уже вошедшим». Без привязки к драйверу.

Формат совместим по смыслу с Playwright `storage_state` (cookies + localStorage по
источникам), но не повторяет его словари: драйвер переводит своё представление сюда и
обратно. Сессия не всегда сводится к кукам — бывает токен или заголовок, который знает только
SPA, — для этого `extras`: его кладёт и читает site SDK, пул только хранит.

Всё содержимое — секреты: значения кук, localStorage и `extras` в `repr` не попадают.
Срок куки — момент на настенных часах, который переживает процесс, поэтому `datetime` в UTC.

`SessionState.parse` принимает сессию в том виде, в каком её приносит оператор: полный
`auth.json` Playwright, JSON-массив кук из расширения браузера, Netscape `cookies.txt` или строку
`имя=значение; имя=значение` из DevTools. Формат угадывается по содержимому, а не по имени
файла: в форму вставляют текст.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from browser_pool._choice import check_choice

if TYPE_CHECKING:
    from collections.abc import Mapping

type SameSite = Literal["Strict", "Lax", "None"]

_SAME_SITE: dict[str, SameSite] = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
}
"""Как `sameSite` пишут разные экспортёры; `unspecified` и прочее — без атрибута."""

_NETSCAPE_FIELDS = 7
_HTTP_ONLY_PREFIX = "#HttpOnly_"


class StateFormatError(ValueError):
    """Строка сессии не разбирается ни в один из известных форматов."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Cookie:
    """Кука со всеми атрибутами: без домена и пути браузер её обратно не примет."""

    name: str
    value: str = field(repr=False)
    domain: str
    path: str = "/"
    expires: datetime | None = None
    """Когда истекает, в UTC. `None` — сессионная: живёт до закрытия браузера."""
    secure: bool = False
    http_only: bool = False
    same_site: SameSite | None = None

    def __post_init__(self) -> None:
        if self.same_site is not None:
            check_choice(self, "same_site", SameSite)
        if not self.name:
            msg = "name куки не может быть пустым"
            raise ValueError(msg)
        if not self.domain:
            msg = f"domain куки {self.name!r} пуст: браузеру некуда её положить"
            raise ValueError(msg)
        if self.expires is not None:
            if self.expires.tzinfo is None:
                msg = f"expires куки {self.name!r} без таймзоны: неизвестно, чей это момент"
                raise ValueError(msg)
            object.__setattr__(self, "expires", self.expires.astimezone(UTC))

    @property
    def is_session(self) -> bool:
        """Сессионная кука: срока нет, живёт до закрытия браузера."""
        return self.expires is None


@dataclass(frozen=True, slots=True, kw_only=True)
class Origin:
    """localStorage одного источника (`схема://хост[:порт]`) — восстанавливается только туда же."""

    origin: str
    local_storage: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if "://" not in self.origin:
            msg = f"origin должен быть вида схема://хост, получено {self.origin!r}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionState:
    """Куки, localStorage и прочее, что делает вход «уже бывшим»."""

    # ast-grep-ignore: secret-field-visible-in-repr — repr самой Cookie скрывает значение, имена кук не секрет
    cookies: tuple[Cookie, ...] = ()
    origins: tuple[Origin, ...] = ()
    extras: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    """Сессия сверх кук — токены, заголовки SPA. Кладёт и читает site SDK, пул только хранит."""

    def __post_init__(self) -> None:
        # Копия и заморозка: снаружи отданный словарь не должен менять уже сохранённое состояние.
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))

    def is_empty(self) -> bool:
        """В состоянии нечего восстанавливать."""
        return not self.cookies and not self.origins and not self.extras

    def with_extras(self, extras: Mapping[str, Any]) -> Self:
        """То же состояние с другими `extras`."""
        return replace(self, extras=extras)

    @classmethod
    def parse(cls, raw: str, *, default_domain: str = "", secure: bool = True) -> Self:
        """Сессия из текста в любом поддерживаемом формате; нечитаемое — `StateFormatError`.

        `default_domain` — домен для кук, у которых его нет (строка заголовка, неполный экспорт);
        чтобы кука ходила и на поддомены, передайте его с точкой: `.example.com`. Форматам, где домен
        записан у каждой куки (`auth.json`, `cookies.txt`, экспорт расширения), он не нужен.
        `secure` — признак для кук из строки `имя=значение; …` (DevTools его не показывает; `True` —
        куки не уйдут по http); в остальных форматах он записан у каждой куки.
        """
        try:
            return cls._parse(raw, default_domain, secure=secure)
        except StateFormatError:
            raise
        except (
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
            OverflowError,
            OSError,
        ) as error:
            # Чужой тип исключения из разбора (дата вне диапазона, не тот тип поля) — тоже «не разобралось».
            msg = f"Сессия не разобралась: {type(error).__name__}"
            raise StateFormatError(msg) from None

    @classmethod
    def _parse(cls, raw: str, default_domain: str, *, secure: bool) -> Self:
        text = raw.strip().removeprefix("\ufeff").strip()
        if not text:
            msg = "Сессия пуста: вставьте auth.json, cookies.txt, JSON-массив кук или строку кук"
            raise StateFormatError(msg)
        if text.startswith("{"):
            return cls._from_playwright(_load_json(text))
        if text.startswith("["):
            return cls(cookies=_from_cookie_list(_load_json(text), default_domain))
        if _looks_like_netscape(text):
            return cls(cookies=_from_netscape(text))
        return cls(cookies=_from_header(text, default_domain, secure=secure))

    @classmethod
    def _from_playwright(cls, parsed: object) -> Self:
        if not isinstance(parsed, dict):
            msg = "Ожидался объект storage_state"
            raise StateFormatError(msg)
        data = cast("dict[str, Any]", parsed)
        cookies: object = data.get("cookies")
        if not isinstance(cookies, list):
            msg = "В storage_state нет списка cookies: это не файл сессии Playwright"
            raise StateFormatError(msg)
        origins: list[Origin] = []
        for item in cast("list[object]", data.get("origins") or []):
            if not isinstance(item, dict):
                continue
            entry = cast("dict[str, Any]", item)
            pairs: object = entry.get("localStorage") or []
            if not isinstance(pairs, list):
                msg = "localStorage источника — не список пар имя/значение"
                raise StateFormatError(msg)
            storage: list[tuple[str, str]] = []
            for pair in cast("list[object]", pairs):
                if isinstance(pair, dict):
                    fields = cast("dict[str, Any]", pair)
                    storage.append((str(fields.get("name", "")), str(fields.get("value", ""))))
            origins.append(
                Origin(origin=str(entry.get("origin", "")), local_storage=tuple(storage))
            )
        return cls(
            cookies=_from_cookie_list(cast("list[object]", cookies), default_domain=""),
            origins=tuple(origins),
        )


def _load_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        msg = f"Сессия не разобралась как JSON: {error.msg} (строка {error.lineno})"
        raise StateFormatError(msg) from error


def _moment(value: object) -> datetime | None:
    """Срок куки из числа секунд эпохи; `-1`, `0` и отсутствие — сессионная кука."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        msg = "Срок куки вне допустимого диапазона дат"
        raise StateFormatError(msg) from None


def _same_site(value: object) -> SameSite | None:
    return _SAME_SITE.get(str(value).casefold()) if value is not None else None


def _from_cookie_list(parsed: object, default_domain: str) -> tuple[Cookie, ...]:
    """Список кук: формат Playwright (`expires`) и экспорт расширений (`expirationDate`, `session`)."""
    if not isinstance(parsed, list):
        msg = "Ожидался JSON-массив cookies"
        raise StateFormatError(msg)
    cookies: list[Cookie] = []
    for item in cast("list[object]", parsed):
        if not isinstance(item, dict):
            msg = f"Элемент массива cookies — не объект: {type(item).__name__}"
            raise StateFormatError(msg)
        entry = cast("dict[str, Any]", item)
        name, value = entry.get("name"), entry.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            continue
        expires = (
            None
            if entry.get("session")
            else _moment(entry.get("expires", entry.get("expirationDate")))
        )
        domain = str(entry.get("domain") or default_domain)
        if not domain:
            msg = "У куки нет домена: он нужен в данных или в default_domain"
            raise StateFormatError(msg)
        cookies.append(
            Cookie(
                name=name,
                value=value,
                domain=domain,
                path=str(entry.get("path") or "/"),
                expires=expires,
                secure=bool(entry.get("secure", False)),
                http_only=bool(entry.get("httpOnly", False)),
                same_site=_same_site(entry.get("sameSite")),
            )
        )
    return tuple(cookies)


def _looks_like_netscape(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return any(
        line.startswith(("# Netscape", _HTTP_ONLY_PREFIX))
        or line.count("\t") >= _NETSCAPE_FIELDS - 1
        for line in lines
    )


def _from_netscape(text: str) -> tuple[Cookie, ...]:
    """`cookies.txt`: `domain, include_subdomains, path, secure, expires, name, value`.

    Строки с префиксом `#HttpOnly_` — не комментарии, а httpOnly-куки (так пишет curl).
    """
    cookies: list[Cookie] = []
    for line in text.splitlines():
        stripped = line.lstrip().rstrip("\r\n")  # хвостовая табуляция — пустое значение куки
        http_only = stripped.startswith(_HTTP_ONLY_PREFIX)
        if http_only:
            stripped = stripped.removeprefix(_HTTP_ONLY_PREFIX)
        if not stripped.strip() or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) < _NETSCAPE_FIELDS:
            continue
        domain, _subdomains, path, secure, expires, name, value = parts[:_NETSCAPE_FIELDS]
        cookies.append(
            Cookie(
                name=name,
                value=value,
                domain=domain,
                path=path or "/",
                expires=_moment(int(expires)) if expires.isdigit() else None,
                secure=secure.upper() == "TRUE",
                http_only=http_only,
            )
        )
    return tuple(cookies)


def _from_header(text: str, default_domain: str, *, secure: bool) -> tuple[Cookie, ...]:
    """Строка `имя=значение; имя=значение` — как в DevTools или заголовке `Cookie`."""
    if not default_domain:
        msg = "Строка кук не несёт домена: укажите default_domain (например, .example.com)"
        raise StateFormatError(msg)
    cookies: list[Cookie] = []
    for part in text.replace("\n", ";").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name.strip():
            cookies.append(
                Cookie(name=name.strip(), value=value.strip(), domain=default_domain, secure=secure)
            )
    if not cookies:
        msg = "Строка кук пуста или не в формате «имя=значение; имя=значение»"
        raise StateFormatError(msg)
    return tuple(cookies)
