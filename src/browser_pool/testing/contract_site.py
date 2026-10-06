"""Локальный сайт с логином и прокси-заглушка — на stdlib, для контрактных тестов драйверов.

Сайт (`ContractSite`), ответы — `text/plain`:

- `/login?user=ada` — ставит куку `sid=ada`, отвечает `ok`; с `&rich=1` кука — со сроком,
  `HttpOnly` и `SameSite=Lax`, как у настоящих сайтов;
- `/whoami` — имя из куки `sid` или `anonymous`;
- `/odd-cookie` — ставит куку без имени (`Set-Cookie: nameless`): браузеры такие хранят;
- `/page` — пустая HTML-страница: на ней тест исполняет свой JS.

Прокси (`ContractProxy`) — HTTP-прокси для `http://`: любой хост направляет на сайт, поэтому
адрес вида `http://contract.test:<порт сайта>/` открывается только через него — так видно, что
прокси контекста действительно используется. Записывает, какие хосты через него ходили; с
`username`/`password` требует `Proxy-Authorization: Basic`.

Оба работают в своём потоке, пока открыт `with`.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar, Protocol, Self, override
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from types import TracebackType

FAKE_HOST = "contract.test"
"""Хост, который резолвит только `ContractProxy`."""

_OK = 200
_NOT_FOUND = 404
_BAD_GATEWAY = 502
_PROXY_AUTH_REQUIRED = 407
_FORWARDED_REQUEST = ("Cookie", "Content-Type")
_FORWARDED_RESPONSE = ("Set-Cookie", "Location", "Content-Type")
_RICH_COOKIE = "Path=/; Max-Age=3600; HttpOnly; SameSite=Lax"
_CHROME_UNSAFE_PORTS = frozenset(
    {1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668}
    | {6669, 6679, 6697, 10080}
)
"""Порты выше 1023, на которые Chromium не ходит (`net::ERR_UNSAFE_PORT`): ОС вправе выдать и такой."""


def safe_port(port: int) -> bool:
    """Откроет ли браузер адрес с этим портом."""
    return port not in _CHROME_UNSAFE_PORTS


class _Quiet(BaseHTTPRequestHandler):
    """Без журнала запросов в stderr: тесты не шумят."""

    @override
    def log_message(self, format: str, *args: object) -> None:
        _ = format, args

    def _reply(
        self,
        status: int,
        body: str,
        headers: dict[str, str] | None = None,
        *,
        content_type: str = "text/plain",
    ) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)


class _SiteHandler(_Quiet):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/login":
            query = parse_qs(parts.query)
            user = query.get("user", ["anonymous"])[0]
            attributes = _RICH_COOKIE if "rich" in query else "Path=/"
            self._reply(_OK, "ok", {"Set-Cookie": f"sid={user}; {attributes}"})
        elif parts.path == "/whoami":
            cookies = SimpleCookie()
            with contextlib.suppress(CookieError):  # кука без имени — не повод не узнать остальные
                cookies.load(_named_only(self.headers.get("Cookie", "")))
            self._reply(_OK, cookies["sid"].value if "sid" in cookies else "anonymous")
        elif parts.path == "/odd-cookie":
            self._reply(_OK, "ok", {"Set-Cookie": "nameless; Path=/"})
        elif parts.path == "/page":
            self._reply(
                _OK,
                "<!doctype html><title>contract</title><body>page</body>",
                content_type="text/html",
            )
        else:
            self._reply(_NOT_FOUND, "not found")


def _named_only(header: str) -> str:
    """Заголовок `Cookie` без кук, у которых нет имени."""
    return "; ".join(part for part in header.split("; ") if "=" in part)


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        while not safe_port(self._server.server_address[1]):
            unsafe = self._server
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            unsafe.server_close()
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


class ContractSite(_Server):
    """Сайт с логином на куке `sid`."""

    def __init__(self) -> None:
        """Слушает свободный порт на 127.0.0.1."""
        super().__init__(_SiteHandler)

    def url(self, path: str, *, host: str = "127.0.0.1") -> str:
        """Адрес страницы сайта; `host=FAKE_HOST` — открывается только через `ContractProxy`."""
        return f"http://{host}:{self.port}{path}"


class HasPort(Protocol):
    """Локальный сервер, куда прокси ведёт все запросы."""

    @property
    def port(self) -> int:
        """Порт на 127.0.0.1."""
        ...


class ContractProxy(_Server):
    """HTTP-прокси, который любой хост ведёт на локальный сайт (`ContractSite` или свой)."""

    def __init__(
        self, site: HasPort, *, username: str | None = None, password: str | None = None
    ) -> None:
        """С `username` требует авторизацию Basic."""
        handler = type(
            "_BoundProxyHandler",
            (_ProxyHandler,),
            {"site_port": site.port, "credentials": _basic(username, password), "owner": self},
        )
        self.hosts: list[str] = []
        """Хосты запросов, прошедших через прокси, по порядку."""
        self.rejected = 0
        """Сколько запросов отклонено без кредов или с чужими."""
        super().__init__(handler)


class _ProxyHandler(_Quiet):
    site_port: ClassVar[int]
    credentials: ClassVar[str | None]
    owner: ClassVar[ContractProxy]

    def do_GET(self) -> None:
        self._forward("GET")

    def do_POST(self) -> None:
        self._forward("POST")

    def _forward(self, method: str) -> None:
        if self.credentials is not None and (
            self.headers.get("Proxy-Authorization") != f"Basic {self.credentials}"
        ):
            self.owner.rejected += 1
            self._reply(
                _PROXY_AUTH_REQUIRED, "auth", {"Proxy-Authenticate": 'Basic realm="contract"'}
            )
            return
        parts = urlsplit(self.path)
        self.owner.hosts.append(parts.hostname or "")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            name: value
            for name in _FORWARDED_REQUEST
            if (value := self.headers.get(name)) is not None
        }
        upstream = http.client.HTTPConnection("127.0.0.1", self.site_port, timeout=10)
        try:
            upstream.request(
                method,
                parts.path + (f"?{parts.query}" if parts.query else ""),
                body=body,
                headers=headers,
            )
            response = upstream.getresponse()
            answer = response.read()
        except OSError:
            self._reply(_BAD_GATEWAY, "upstream")
            return
        finally:
            upstream.close()
        # Браузер вправе уйти, не дочитав ответ (переход перебил загрузку): это не ошибка прокси.
        with contextlib.suppress(ConnectionError):
            self.send_response(response.status)
            for name in _FORWARDED_RESPONSE:
                for value in response.headers.get_all(name) or []:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)


def _basic(username: str | None, password: str | None) -> str | None:
    if username is None:
        return None
    return base64.b64encode(f"{username}:{password or ''}".encode()).decode()


__all__ = ["FAKE_HOST", "ContractProxy", "ContractSite", "HasPort", "safe_port"]
