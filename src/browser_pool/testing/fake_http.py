"""Сеть фейкового браузера: настоящий HTTP через `urllib` — с куками и прокси контекста.

Хватает, чтобы на фейке проверить то же, что на настоящем браузере: куки доходят до сайта и
изолированы между контекстами, прокси контекста действительно используется, авторизация на
прокси проходит, мёртвый прокси — это сбой прокси, а не сайта. Только `http`: сайт и прокси
тестов живут локально.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from browser_pool.clock import utc_now
from browser_pool.state import Cookie

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from browser_pool.proxies import Proxy
    from browser_pool.state import SameSite

_PROXY_AUTH_REQUIRED = 407
_SAME_SITE: dict[str, SameSite] = {"strict": "Strict", "lax": "Lax", "none": "None"}
_TIMEOUT = 10.0


class FakeNetworkError(Exception):
    """Сайт не ответил: как `net::ERR_CONNECTION_REFUSED` у Chromium."""


class FakeProxyError(FakeNetworkError):
    """Прокси не пропустил: не отвечает или не принял креды."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Ответ сайта: тело текстом и куки, которые он поставил."""

    body: str = field(repr=False)
    cookies: tuple[Cookie, ...] = field(repr=False)


def fetch(url: str, *, proxy: Proxy | None, cookies: Iterable[Cookie]) -> FetchResult:
    """GET `url` — блокирующе, зовётся из потока. Куки отправляются те, что видны хосту и пути."""
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path or "/"
    header = "; ".join(
        f"{cookie.name}={cookie.value}"
        for cookie in cookies
        if _visible(cookie.domain, host) and path.startswith(cookie.path)
    )
    request = urllib.request.Request(url, headers={"Cookie": header} if header else {})  # noqa: S310 — только http тестового сайта
    # Прокси — только контекста: переменные окружения машины тестов значения не имеют.
    handler = urllib.request.ProxyHandler({"http": proxy.url} if proxy is not None else {})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(request, timeout=_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            raw_cookies: list[str] = response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as error:
        error.close()  # ответ с телом: не закрыть — сокет и временный файл повиснут до сборщика
        if error.code == _PROXY_AUTH_REQUIRED:
            msg = "прокси не принял креды (407)"
            raise FakeProxyError(msg) from None
        msg = f"сайт ответил {error.code}"
        raise FakeNetworkError(msg) from None
    except (urllib.error.URLError, OSError) as error:
        if proxy is not None:
            msg = f"прокси {proxy.label} не отвечает"
            raise FakeProxyError(msg) from error
        msg = f"сайт {host} не отвечает"
        raise FakeNetworkError(msg) from error
    return FetchResult(body=body, cookies=tuple(_parse_set_cookie(raw_cookies, host)))


def _visible(cookie_domain: str, host: str) -> bool:
    owner = cookie_domain.lstrip(".").lower()
    return host.lower() == owner or host.lower().endswith("." + owner)


def _parse_set_cookie(headers: Iterable[str], host: str) -> list[Cookie]:
    cookies: list[Cookie] = []
    for header in headers:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except CookieError:
            continue
        for name, morsel in parsed.items():
            cookies.append(
                Cookie(
                    name=name,
                    value=morsel.value,
                    domain=morsel["domain"] or host,
                    path=morsel["path"] or "/",
                    expires=_expires(morsel["max-age"]),
                    secure=bool(morsel["secure"]),
                    http_only=bool(morsel["httponly"]),
                    same_site=_SAME_SITE.get(str(morsel["samesite"]).lower()),
                )
            )
    return cookies


def _expires(max_age: str) -> datetime | None:
    """Срок куки из `Max-Age`; без него кука сессионная."""
    return utc_now() + timedelta(seconds=int(max_age)) if max_age.isdigit() else None


__all__ = ["FakeNetworkError", "FakeProxyError", "FetchResult", "fetch"]
