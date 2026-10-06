"""Удалённый CDP: браузеры, которые уже запущены где-то ещё.

Свой Chrome с `--remote-debugging-port`, browserless, облачный Chromium — всё, куда можно
подключиться по CDP. Пул не запускает и не останавливает эти браузеры: `start` выдаёт слоту
пула адрес, `stop` возвращает его в запас. Один адрес — один слот: два слота пула в одном
удалённом браузере мешали бы друг другу, поэтому браузеров в пуле не больше, чем адресов.

    pool = BrowserPool(PlaywrightDriver(), provider=RemoteCDP("http://10.0.0.5:9222"))

Адрес `http(s)://host:port` провайдер превращает в WebSocket-адрес браузера (`/json/version`) —
его понимают все драйверы; адрес `ws(s)://…` отдаётся как есть. Хост в ответе браузера
заменяется хостом из адреса: удалённый Chrome называет себя `localhost`. Путь и query адреса
(`?token=…`) сохраняются и в запросе, и в выданном WebSocket-адресе; запрос идёт мимо системного
прокси, а ошибки не повторяют адрес — в нём токен.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit, urlunsplit

from browser_pool.driver import Endpoint
from browser_pool.errors import NoFreeEndpointError

if TYPE_CHECKING:
    from browser_pool.provider import EndpointRequest


class RemoteCDP:
    """Провайдер уже запущенных браузеров по CDP. Реализует `EndpointProvider`."""

    def __init__(self, *addresses: str, timeout: float = 10.0) -> None:
        """`addresses` — `http(s)://host:port` или `ws(s)://…/devtools/browser/…`, по одному на слот.

        `timeout` — сколько ждать ответа `/json/version`, секунды.
        """
        if not addresses:
            msg = "RemoteCDP без адресов: нужен хотя бы один"
            raise ValueError(msg)
        unknown = [a for a in addresses if urlsplit(a).scheme not in {"http", "https", "ws", "wss"}]
        if unknown:
            msg = (
                f"RemoteCDP понимает http(s):// и ws(s)://, получено {len(unknown)} других адресов"
            )
            raise ValueError(msg)
        self._addresses = tuple(addresses)
        self._timeout = timeout
        self._busy: set[str] = set()
        self._issued: dict[str, str] = {}
        """WebSocket-адрес выданного эндпоинта → адрес из списка."""

    async def start(self, request: EndpointRequest) -> Endpoint:
        """Свободный адрес — предпочтительно тот, что по номеру слота."""
        address = self._take(request.browser_id)
        try:
            url = await self._resolve(address)
        except BaseException:
            self._busy.discard(address)
            raise
        self._issued[url] = address
        return Endpoint(kind="cdp", url=url)

    async def stop(self, endpoint: Endpoint) -> None:
        """Вернуть адрес в запас. Удалённый браузер не наш — его не закрываем."""
        await asyncio.sleep(0)
        address = self._issued.pop(endpoint.url, None)
        if address is not None:
            self._busy.discard(address)

    async def reap_orphans(self) -> int:
        """Сирот не бывает: удалённые браузеры пулу не принадлежат."""
        await asyncio.sleep(0)
        return 0

    def _take(self, browser_id: str) -> str:
        suffix = browser_id.rpartition("-")[2]
        index = int(suffix) if suffix.isdigit() else 0
        preferred = self._addresses[index % len(self._addresses)]
        free = [a for a in self._addresses if a not in self._busy]
        if not free:
            raise NoFreeEndpointError(addresses=len(self._addresses))
        address = preferred if preferred in free else free[0]
        self._busy.add(address)
        return address

    async def _resolve(self, address: str) -> str:
        parts = urlsplit(address)
        if parts.scheme in {"ws", "wss"}:
            return address
        # Путь и query адреса сохраняются: `…/prefix?token=X` → `…/prefix/json/version?token=X`.
        version = urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/") + "/json/version", parts.query, "")
        )
        raw = await asyncio.to_thread(_fetch, version, self._timeout)
        try:
            ws = urlsplit(cast("str", json.loads(raw)["webSocketDebuggerUrl"]))
        except (ValueError, KeyError, TypeError):
            msg = "RemoteCDP: ответ /json/version без webSocketDebuggerUrl"
            raise ConnectionError(msg) from None
        scheme = "wss" if parts.scheme == "https" else "ws"
        # Токен из адреса нужен и WebSocket'у: облачные браузеры не кладут его в свой ответ.
        query = "&".join(part for part in (ws.query, parts.query) if part)
        return urlunsplit((scheme, parts.netloc, ws.path, query, ws.fragment))


def _fetch(url: str, timeout: float) -> bytes:
    """GET по адресу, который прошёл проверку схемы в конструкторе; мимо системного прокси.

    Текст ошибки не повторяет адрес: в нём бывает токен (`?token=`).
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return cast("bytes", response.read())
    except urllib.error.HTTPError as error:
        error.close()  # тело ответа не нужно, а незакрытое — предупреждение при сборке мусора
        msg = f"RemoteCDP: /json/version ответил кодом {error.code}"
        raise ConnectionError(msg) from None
    except (urllib.error.URLError, OSError) as error:
        msg = f"RemoteCDP: адрес не ответил ({type(error).__name__})"
        raise ConnectionError(msg) from None


__all__ = ["RemoteCDP"]
