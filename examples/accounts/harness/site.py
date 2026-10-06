"""Сайт демо: почта с входом по паролю — на stdlib, в своём потоке.

- `GET /login` — форма входа; `POST /login` — проверка пароля, кука `sid`, переход в `/inbox`.
- `GET /inbox` — ящик вошедшего (`<h1 id="owner">`), без сессии — на `/login`.

Сайт считает входы по паролю (`password_logins`): демо проверяет, что после падения браузера
пул восстановил сессии из хранилища, а не вошёл заново.
"""

from __future__ import annotations

import html
import secrets
import threading
from collections import Counter
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar, Self, override
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

HOST = "mail.demo"
"""Хост сайта. Резолвят его только прокси демо: без прокси сайт не открыть."""

_OK, _FOUND, _UNAUTHORIZED = 200, 302, 401

LOGIN_FORM = """<!doctype html><title>Вход</title>
<form method="post" action="/login">
  <input name="login" id="login"><input name="password" id="password" type="password">
  <button id="submit">Войти</button>
</form>"""


class DemoSite:
    """Сайт с аккаунтами `{логин: пароль}` и счётом входов по паролю."""

    def __init__(self, accounts: Mapping[str, str]) -> None:
        """Слушает свободный порт на 127.0.0.1."""
        self.accounts = dict(accounts)
        self.password_logins: Counter[str] = Counter()
        """Сколько раз каждый аккаунт входил по паролю."""
        self.sessions: dict[str, str] = {}
        """Токен сессии → логин."""
        self.lock = threading.Lock()
        handler = type("_BoundHandler", (_Handler,), {"site": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        """Порт на 127.0.0.1 — туда ведут прокси демо."""
        return self._server.server_address[1]

    def url(self, path: str) -> str:
        """Адрес страницы по имени сайта: открывается только через прокси демо."""
        return f"http://{HOST}:{self.port}{path}"

    def login(self, login: str, password: str) -> str | None:
        """Вход по паролю: токен новой сессии или `None`."""
        with self.lock:
            if self.accounts.get(login) != password:
                return None
            self.password_logins[login] += 1
            token = secrets.token_urlsafe(16)
            self.sessions[token] = login
            return token

    def owner(self, token: str | None) -> str | None:
        """Чья сессия."""
        with self.lock:
            return self.sessions.get(token or "")

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


class _Handler(BaseHTTPRequestHandler):
    site: ClassVar[DemoSite]

    @override
    def log_message(self, format: str, *args: object) -> None:
        _ = format, args

    def do_GET(self) -> None:
        if self.path.startswith("/login"):
            self._page(_OK, LOGIN_FORM)
            return
        owner = self.site.owner(self._sid())
        if owner is None:
            self._redirect("/login")
            return
        self._page(
            _OK, f'<!doctype html><title>Почта</title><h1 id="owner">{html.escape(owner)}</h1>'
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        token = self.site.login(form.get("login", [""])[0], form.get("password", [""])[0])
        if token is None:
            self._page(_UNAUTHORIZED, "<!doctype html><title>Ошибка</title><p>неверный пароль</p>")
            return
        self._redirect("/inbox", cookie=f"sid={token}; Path=/; HttpOnly")

    def _sid(self) -> str | None:
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        return cookies["sid"].value if "sid" in cookies else None

    def _page(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, *, cookie: str | None = None) -> None:
        self.send_response(_FOUND)
        self.send_header("Location", location)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()
