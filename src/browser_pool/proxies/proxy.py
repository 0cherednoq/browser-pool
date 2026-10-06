"""Прокси как значение: куда подключаться и с какими кредами.

Креды — секрет, а прокси попадает в ошибки, события и логи пула. Поэтому наружу в
текстовом виде прокси показывается только безопасным именем (`label`), а полный адрес с
кредами (`url`) отдаётся лишь явным обращением — драйверу, которому он нужен для
подключения. Строки из списков прокси разбирает `Proxy.parse`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import override
from urllib.parse import quote, unquote

SCHEMES = frozenset({"http", "https", "socks4", "socks5"})
"""Схемы, которые понимает пул. Что из них умеет конкретный SDK — дело его драйвера."""

_MAX_PORT = 65535
_FORBIDDEN_IN_HOST = frozenset("/@ ")
_SCHEME_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
_PAIR = 2
_QUAD = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class Proxy:
    """Адрес прокси и креды к нему.

    `id` — идентификатор у источника (например, строка в БД приложения): по нему вызывающий
    узнаёт, какой прокси пометить сбойным. Прокси без `id` называется своим адресом. Хост
    хранится в нижнем регистре: `Proxy.parse` и конструктор дают один и тот же прокси.
    """

    host: str
    port: int
    scheme: str = "http"
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.host or _FORBIDDEN_IN_HOST & set(self.host):
            msg = f"host прокси должен быть голым адресом без схемы и кредов: {self.host!r}"
            raise ValueError(msg)
        if not 0 < self.port <= _MAX_PORT:
            msg = f"port прокси вне 1..{_MAX_PORT}: {self.port}"
            raise ValueError(msg)
        if self.scheme not in SCHEMES:
            msg = f"схема прокси {self.scheme!r} не поддерживается: {', '.join(sorted(SCHEMES))}"
            raise ValueError(msg)
        if self.password is not None and self.username is None:
            msg = "password прокси без username: такие креды никто не примет"
            raise ValueError(msg)
        object.__setattr__(self, "host", self.host.lower())

    @property
    def server(self) -> str:
        """Адрес без кредов: `scheme://host:port`."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def url(self) -> str:
        """Адрес с кредами — для подключения. В логи и сообщения не выводить."""
        if self.username is None:
            return self.server
        credentials = quote(self.username, safe="")
        if self.password is not None:
            credentials += ":" + quote(self.password, safe="")
        scheme, _, rest = self.server.partition("://")
        return f"{scheme}://{credentials}@{rest}"

    @property
    def has_auth(self) -> bool:
        """Нужна ли авторизация на прокси."""
        return self.username is not None

    @property
    def label(self) -> str:
        """Безопасное имя для ошибок, событий и логов: `id` источника или адрес без кредов.

        Прокси с логином и без `id` получают к адресу короткую метку логина (`…:7000#3fa9c1d2`):
        шлюз резидентных прокси один, а сессии у него разные, и называться они должны по-разному.
        Сам логин в имя не попадает, пароль не участвует вовсе.
        """
        if self.id is not None:
            return self.id
        if self.username is None:
            return self.server
        mark = hashlib.sha256(self.username.encode()).hexdigest()[:8]
        return f"{self.server}#{mark}"

    @override
    def __str__(self) -> str:
        return self.label

    @classmethod
    def parse(cls, line: str, *, id: str | None = None) -> Proxy:  # noqa: A002 — имя поля значения
        """Прокси из строки в одном из принятых видов; неоднозначное — `ProxyFormatError`.

        Принимаются:

        - `host:port` и `scheme://[user:pass@]host:port` (хвостовой `/` после порта допустим);
        - `user:pass@host:port` — как в URL: креды percent-encoded (`%40` — `@`, `%25` — `%`),
          пароль может содержать `@` и `:`, последний `@` отделяет адрес;
        - `host:port@user:pass` — вид продавцов с адресом впереди (креды тоже percent-encoded);
        - `host:port:user:pass` и `user:pass:host:port` — двоеточечные, без декодирования: пароль
          берётся как есть, но не может содержать `:` (пароль со спецсимволами — только в виде URL).
          Если вид не определить по портам и IP-адресам, строка отвергается, а не угадывается.

        IPv6 — в квадратных скобках. Схема без указания — `http`. Хост приводится к нижнему
        регистру. Текст ошибки не содержит ни одного фрагмента строки: в ней пароль.
        """
        text = line.strip()
        if not text:
            msg = "строка прокси пуста"
            raise ProxyFormatError(msg)
        found = _SCHEME_PREFIX.match(text)
        scheme = found.group(1).lower() if found else "http"
        rest = text[found.end() :] if found else text
        if scheme not in SCHEMES:
            msg = "схема прокси не поддерживается (http, https, socks4, socks5)"
            raise ProxyFormatError(msg)
        if "@" in rest:
            return _parse_at_form(rest, scheme=scheme, explicit_scheme=found is not None, id=id)
        host, port, username, password = _split_colon_form(rest)
        return _build(
            host=host, port=port, scheme=scheme, username=username, password=password, id=id
        )


class ProxyFormatError(ValueError):
    """Строку не удалось понять как прокси. Сообщение не содержит кредов."""


def _port_like(text: str) -> bool:
    return text.isascii() and text.isdecimal() and 0 < int(text) <= _MAX_PORT


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _split_address(address: str) -> tuple[str, str] | None:
    """`host:port` или `[ipv6]:port` → хост и порт текстом; `None` — это не адрес."""
    address = address.removesuffix("/")
    if address.startswith("["):
        host, closed, tail = address[1:].partition("]")
        if not closed or not tail.startswith(":"):
            return None
        return host, tail[1:]
    host, has_port, port = address.rpartition(":")
    if not has_port or not host:
        return None
    return host, port


def _parse_at_form(rest: str, *, scheme: str, explicit_scheme: bool, id: str | None) -> Proxy:  # noqa: A002
    """Виды с `@`: `user:pass@host:port` и `host:port@user:pass`."""
    left, _, right = rest.rpartition("@")
    address_first = False
    if not explicit_scheme:
        left_split, right_split = _split_address(left), _split_address(right)
        left_is = left_split is not None and _port_like(left_split[1])
        right_is = right_split is not None and _port_like(right_split[1])
        if left_split is not None and left_is and not right_is:
            address_first = True
        elif left_split is not None and right_split is not None and left_is and right_is:
            # `1.2.3.4:8080@user:12345` против `user:12345@host:8080`: IP-адрес — признак адреса.
            address_first = _is_ipv4(left_split[0]) and not _is_ipv4(right_split[0])
        if not address_first and _is_ipv4(left.partition(":")[0]):
            msg = (
                "похоже на host:port:user:pass с символом @ в пароле: "
                "такой пароль записывается как scheme://user:pass@host:port, спецсимволы — %-кодами"
            )
            raise ProxyFormatError(msg)
    address, credentials = (left, right) if address_first else (right, left)
    split = _split_address(address)
    if split is None:
        msg = "у прокси не указан порт или адрес записан неверно (IPv6 — в скобках)"
        raise ProxyFormatError(msg)
    host, port = split
    username, _, password = credentials.partition(":")
    return _build(
        host=host,
        port=port,
        scheme=scheme,
        username=unquote(username) or None,
        password=unquote(password) or None,
        id=id,
    )


def _split_colon_form(rest: str) -> tuple[str, str, str | None, str | None]:
    """`host:port`, `host:port:user:pass` или `user:pass:host:port` (IPv6 — в скобках)."""
    bracketed = rest.startswith("[")
    if bracketed:
        host, closed, tail = rest[1:].partition("]")
        if not closed or not tail.startswith(":"):
            msg = "IPv6-адрес прокси нужен в виде [адрес]:порт"
            raise ProxyFormatError(msg)
        parts = [host, *tail[1:].split(":")]
    else:
        parts = rest.split(":")
        if len(parts) == _PAIR:
            parts[1] = parts[1].removesuffix("/")  # `scheme://host:port/` — путь пуст
    match parts:
        case [_]:
            msg = "у прокси не указан порт"
            raise ProxyFormatError(msg)
        case [host, port]:
            return host, port, None, None
        case [first, second, third, fourth]:
            return _split_quad([first, second, third, fourth], bracketed=bracketed)
        case _:
            msg = (
                "непонятный формат прокси: ждём host:port, host:port:user:pass, user:pass:host:port, "
                "user:pass@host:port, host:port@user:pass или scheme://…; "
                "пароль с ':' записывается как scheme://user:pass@host:port"
            )
            raise ProxyFormatError(msg)


def _split_quad(parts: list[str], *, bracketed: bool) -> tuple[str, str, str | None, str | None]:
    """Четыре части: `host:port:user:pass` или `user:pass:host:port` — по портам и IP."""
    first, second, third, fourth = parts
    host_first = _port_like(second)
    vendor = not bracketed and _port_like(fourth)
    if host_first and vendor:
        host_first, vendor = _decide(first, third)
    if host_first:
        return first, second, third or None, fourth or None
    if vendor:
        return third, fourth, first or None, second or None
    msg = "порт прокси должен быть числом 1..65535"
    raise ProxyFormatError(msg)


def _decide(first: str, third: str) -> tuple[bool, bool]:
    """Оба вида возможны: по IP-адресу — какой из краёв хост; иначе строку отвергаем."""
    first_ip, third_ip = _is_ipv4(first), _is_ipv4(third)
    if first_ip and not third_ip:
        return True, False
    if third_ip and not first_ip:
        return False, True
    msg = (
        "вид прокси неоднозначен (host:port:user:pass или user:pass:host:port): "
        "запишите как scheme://user:pass@host:port"
    )
    raise ProxyFormatError(msg)


def _build(
    *,
    host: str,
    port: str,
    scheme: str,
    username: str | None,
    password: str | None,
    id: str | None,  # noqa: A002
) -> Proxy:
    if not host or _FORBIDDEN_IN_HOST & set(host):
        msg = "адрес прокси пуст или содержит недопустимые символы (пробел, /, @)"
        raise ProxyFormatError(msg)
    if not _port_like(port):
        msg = "порт прокси должен быть числом 1..65535"
        raise ProxyFormatError(msg)
    if password is not None and username is None:
        msg = "указан пароль прокси без логина"
        raise ProxyFormatError(msg)
    return Proxy(
        host=host, port=int(port), scheme=scheme, username=username, password=password, id=id
    )
