"""AdsPower: профиль антидетекта арендуется как identity.

Браузер — профиль AdsPower: отпечаток, прокси и куки у вендора. Пул поднимает профиль через
локальный API AdsPower, подключается к нему драйвером (Playwright, pydoll — по CDP) и работает в
готовом контексте профиля, а не в новом (`ContextSpec.reuse_default`)::

    provider = AdsPowerProvider(api_key="…", profile=lambda identity: identity.payload.ads_id)
    pool = BrowserPool(PlaywrightDriver(), provider=provider)
    async with pool.page(Identity(key="ads:k1a2b3", proxy=ProxyPolicy.external())) as lease:
        ...

Что провайдер обещает:

- возможности пары «драйвер + AdsPower» (`adapt`): браузер = профиль одной identity, прокси и
  отпечаток у вендора, состояние сессии пул не сохраняет и не восстанавливает — его хранит
  AdsPower;
- все вызовы API — по одному и не чаще `requests_per_second` (AdsPower: 1 запрос в секунду):
  старт профиля тяжёлый, параллельные старты API отвергает;
- профили, которые поднял провайдер, записываются в реестр (`registry`, JSON-файл) до вызова API
  (ответ мог потеряться, а профиль — открыться) вместе с владельцем-процессом: после краха
  процесса `reap_orphans` при следующем старте пула закроет те, чей владелец мёртв. Профили живого
  процесса (в том числе другого пула на том же реестре), открытые человеком и чужие не трогаются;
- отмена или сбой `start` после вызова API закрывают профиль: он не остаётся открытым без хозяина;
- ошибки транспорта и ответ не в JSON — `AdsPowerError`; вызовы локального API идут мимо системного
  прокси (`HTTP_PROXY` для них не нужен и ломает их).

Локальный API — v1 (`/api/v1/browser/start|stop|active`, ключ — `Authorization: Bearer`), адрес
по умолчанию `http://local.adspower.net:50325`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override

from browser_pool.clock import monotonic
from browser_pool.driver import Endpoint
from browser_pool.locks import FileLock
from browser_pool.procguard import process_token

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from browser_pool.driver import DriverCapabilities
    from browser_pool.identity import Identity
    from browser_pool.provider import EndpointRequest

type Transport = Callable[[str, Mapping[str, str], float], Awaitable[bytes]]
"""GET по адресу с заголовками и таймаутом → тело ответа. Подменяется в тестах."""

_logger = logging.getLogger(__name__)

DEFAULT_API = "http://local.adspower.net:50325"


class AdsPowerError(RuntimeError):
    """API AdsPower ответил ошибкой (`code != 0`). Сообщение — от AdsPower, без ключа API."""

    code: int
    """Код ответа AdsPower; `-1` — ответа не было или он непонятен."""
    answered: bool
    """AdsPower ответил отказом (`code != 0`): действие не выполнено, откатывать нечего."""

    def __init__(self, *, code: int, message: str, action: str, answered: bool = False) -> None:
        self.code = code
        self.answered = answered
        super().__init__(f"AdsPower: {action} не удалось (code={code}): {message}")


class AdsPowerProvider:
    """Профили AdsPower как браузеры пула. Реализует `ProfileProvider`."""

    def __init__(
        self,
        *,
        profile: Callable[[Identity], str] | None = None,
        api: str = DEFAULT_API,
        api_key: str | None = None,
        requests_per_second: float = 1.0,
        launch_args: Sequence[str] = (),
        registry: Path | str | None = None,
        timeout: float = 60.0,
        transport: Transport | None = None,
    ) -> None:
        """`profile` — `user_id` профиля AdsPower для identity; по умолчанию — ключ identity.

        `requests_per_second` — потолок частоты вызовов API; `launch_args` — флаги Chrome профиля
        сверх спецификации пула; `registry` — файл реестра поднятых профилей (без него
        `reap_orphans` ничего не находит); `timeout` — ожидание ответа API, секунды.
        """
        if requests_per_second <= 0:
            msg = f"requests_per_second должен быть положительным, получено {requests_per_second}"
            raise ValueError(msg)
        self._profile = profile if profile is not None else _key
        self._api = api.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._interval = 1.0 / requests_per_second
        self._launch_args = tuple(launch_args)
        self._registry = Path(registry) if registry is not None else None
        self._timeout = timeout
        self._transport: Transport = transport if transport is not None else _urllib_get
        self._lock = asyncio.Lock()
        self._registry_lock = asyncio.Lock()
        self._cleanups: set[asyncio.Task[None]] = set()
        self._next_call = 0.0
        self._issued: dict[str, str] = {}
        """WebSocket-адрес выданного эндпоинта → `user_id` профиля."""

    @override
    def __repr__(self) -> str:
        """Без ключа API."""
        return f"AdsPowerProvider(api={self._api!r})"

    def adapt(self, capabilities: DriverCapabilities) -> DriverCapabilities:
        """Браузер — профиль одной identity; прокси, отпечаток и состояние сессии — у AdsPower."""
        return dataclasses.replace(
            capabilities,
            proxy_scope="external",
            can_new_context=False,
            fingerprint_scope="external",
            state_support="none",
            persistent_dir=False,
            new_window=False,
        )

    async def start(self, request: EndpointRequest) -> Endpoint:
        """Поднять профиль identity и отдать адрес CDP его браузера."""
        if request.identity is None:
            msg = "AdsPower поднимает профиль identity, а заявка без identity"
            raise ValueError(msg)
        user_id = self._profile(request.identity)
        params = {"user_id": user_id, "open_tabs": "1", "ip_tab": "0"}
        if request.spec.headless:
            params["headless"] = "1"
        arguments = [*self._launch_args, *request.spec.args]
        if arguments:
            params["launch_args"] = json.dumps(arguments)
        action = f"старт профиля {user_id}"
        # В реестр — до вызова: ответ может потеряться (отмена, таймаут), а профиль — уже открыться.
        await self._remember(user_id)
        try:
            data = await self._call("/api/v1/browser/start", params, action=action)
            ws = cast("dict[str, Any]", data.get("ws") or {})
            url = ws.get("puppeteer")
            if not isinstance(url, str) or not url:
                msg = f"AdsPower не вернул адрес CDP профиля {user_id}"
                raise AdsPowerError(code=-1, message=msg, action=action)  # noqa: TRY301 — общий откат ниже
        except BaseException as error:
            if isinstance(error, AdsPowerError) and error.answered:
                await self._forget(
                    user_id
                )  # профиль не поднялся: откатывать, кроме реестра, нечего
            else:
                await self._close_after_failed_start(user_id)
            raise
        webdriver = data.get("webdriver")
        self._issued[url] = user_id
        return Endpoint(
            kind="cdp",
            url=url,
            driver_path=Path(webdriver) if isinstance(webdriver, str) and webdriver else None,
        )

    async def stop(self, endpoint: Endpoint) -> None:
        """Закрыть профиль. Не вышло — профиль остаётся в реестре до `reap_orphans`."""
        user_id = self._issued.pop(endpoint.url, None)
        if user_id is None:
            return
        await self._stop(user_id)

    async def reap_orphans(self) -> int:
        """Закрыть профили из реестра, оставшиеся открытыми после умершего процесса.

        Профили живого процесса (этого или другого) не трогаются; сбой по одному профилю не обрывает
        остальные — он остаётся в реестре до следующего раза.
        """
        stopped = 0
        for user_id, owner in (await self._read_registry()).items():
            if _owner_alive(owner):
                continue
            try:
                data = await self._call(
                    "/api/v1/browser/active", {"user_id": user_id}, action=f"статус {user_id}"
                )
                if data.get("status") == "Active":
                    await self._stop(user_id)
                    stopped += 1
                else:
                    await self._forget(user_id)
            except Exception:  # noqa: BLE001 — один профиль не должен остановить уборку остальных
                _logger.warning("AdsPower: профиль %s не прибран, остаётся в реестре", user_id)
        return stopped

    # --- внутреннее --------------------------------------------------------------------

    async def _close_after_failed_start(self, user_id: str) -> None:
        """Откат старта: закрыть профиль, который мог открыться. Не бросает и переживает отмену."""
        task = asyncio.ensure_future(self._stop_quietly(user_id))
        self._cleanups.add(task)
        task.add_done_callback(self._cleanups.discard)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    async def _stop_quietly(self, user_id: str) -> None:
        try:
            await self._stop(user_id)
        except Exception:  # noqa: BLE001 — профиль остаётся в реестре, его закроет `reap_orphans`
            _logger.warning("AdsPower: профиль %s не закрылся после неудачного старта", user_id)

    async def _stop(self, user_id: str) -> None:
        await self._call("/api/v1/browser/stop", {"user_id": user_id}, action=f"стоп {user_id}")
        await self._forget(user_id)

    async def _call(self, path: str, params: Mapping[str, str], *, action: str) -> dict[str, Any]:
        """Вызов API: по одному и не чаще потолка. Ответ `code != 0` — `AdsPowerError`."""
        url = f"{self._api}{path}?{urllib.parse.urlencode(params)}"
        async with self._lock:
            wait = self._next_call - monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                raw = await self._transport(url, self._headers, self._timeout)
            except Exception as error:  # noqa: BLE001 — любой сбой транспорта
                # Ни адреса с ключом, ни текста чужого исключения: сообщение — от провайдера.
                raise AdsPowerError(
                    code=-1, message=f"нет ответа API ({type(error).__name__})", action=action
                ) from None
            finally:
                self._next_call = monotonic() + self._interval
        try:
            answer = cast("dict[str, Any]", json.loads(raw))
            code = int(answer.get("code", -1))
        except (ValueError, TypeError, AttributeError):
            raise AdsPowerError(code=-1, message="ответ API — не JSON", action=action) from None
        if code != 0:
            raise AdsPowerError(
                code=code, message=str(answer.get("msg", "")), action=action, answered=True
            )
        return cast("dict[str, Any]", answer.get("data") or {})

    async def _remember(self, user_id: str) -> None:
        if self._registry is None:
            return
        owner: dict[str, Any] = {"pid": os.getpid(), "token": process_token(os.getpid())}
        async with self._registry_guard():
            known = await self._read_registry()
            known[user_id] = owner
            await self._write_registry(known)

    async def _forget(self, user_id: str) -> None:
        if self._registry is None:
            return
        async with self._registry_guard():
            known = await self._read_registry()
            if known.pop(user_id, _ABSENT) is not _ABSENT:
                await self._write_registry(known)

    @contextlib.asynccontextmanager
    async def _registry_guard(self) -> AsyncGenerator[None]:
        """Чтение-изменение-запись реестра: свои вызовы — по очереди, чужие процессы — файловым замком."""
        assert self._registry is not None  # noqa: S101 — вызывается только при заданном реестре
        file_lock = FileLock(self._registry.with_suffix(self._registry.suffix + ".lock"))
        async with self._registry_lock:
            await file_lock.acquire()
            try:
                yield
            finally:
                file_lock.release()

    async def _read_registry(self) -> dict[str, dict[str, Any] | None]:
        registry = self._registry
        if registry is None:
            return {}
        return await asyncio.to_thread(_read_entries, registry)

    async def _write_registry(self, entries: dict[str, dict[str, Any] | None]) -> None:
        if self._registry is not None:
            await asyncio.to_thread(_write_entries, self._registry, entries)


def _key(identity: Identity) -> str:
    return identity.key


async def _urllib_get(url: str, headers: Mapping[str, str], seconds: float) -> bytes:
    return await asyncio.to_thread(_get, url, dict(headers), seconds)


def _get(url: str, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 — адрес API из конфига
    # Локальный API — мимо системного прокси: `HTTP_PROXY` для него не нужен и ломает вызов.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return cast("bytes", response.read())


_ABSENT = object()


def _owner_alive(owner: dict[str, Any] | None) -> bool:
    """Жив ли процесс, записавший профиль в реестр; нет владельца в записи — считается мёртвым."""
    if owner is None:
        return False
    pid, token = owner.get("pid"), owner.get("token")
    if not isinstance(pid, int):
        return False
    current = process_token(pid)
    return current is not None and (token is None or current == token)


def _read_entries(path: Path) -> dict[str, dict[str, Any] | None]:
    """Реестр: `{user_id: {"pid", "token"}}`; прежний вид — список `user_id` — читается как записи без владельца."""
    with contextlib.suppress(FileNotFoundError):
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            return {str(item): None for item in cast("list[object]", loaded)}
        if isinstance(loaded, dict):
            entries = cast("dict[str, object]", loaded)
            return {
                str(user_id): cast("dict[str, Any]", owner) if isinstance(owner, dict) else None
                for user_id, owner in entries.items()
            }
    return {}


def _write_entries(path: Path, entries: dict[str, dict[str, Any] | None]) -> None:
    """Атомарно: реестр читает следующий процесс, полузаписанный файл его обманул бы."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(entries), encoding="utf-8")
    temporary.replace(path)


__all__ = ["DEFAULT_API", "AdsPowerError", "AdsPowerProvider", "Transport"]
