"""Физические ресурсы: настоящие браузеры, контексты и вкладки за логическими арендами.

Планировщик решает «кому что выдать», этот слой делает так, чтобы за выданной арендой стояла
настоящая вкладка:

- браузер запускается лениво, при первой аренде в нём, — один раз даже под одновременными
  арендами; упавший запуск браузер не запоминает: следующая аренда пробует снова;
- контекст identity открывается один раз на поколение — и не открывается тоже один раз: сбой запуска
  или открытия получают все аренды поколения, выданные до сбоя, а не пробуют по очереди; следующая
  попытка — у нового поколения. Аренда более нового поколения (смена
  варианта) закрывает старый контекст и открывает новый, более старого — `StaleLeaseError`;
- вкладки контекста — `TabPool`; если браузер упёрся в лимит открытых вкладок, а у контекста
  своей тёплой нет, закрывается самая холодная тёплая вкладка соседа;
- закрытие — под таймаутом и без исключений: сбой учитывается в `close_failures`, а браузер,
  который не закрылся штатно, добивается `kill_browser`;
- браузер с владельцем (`owner`) запускается под контекст: сначала выбирается прокси,
  потом браузер запускается с ним. Другой прокси (`proxy_scope="browser"`), другой владелец или
  драйвер без `can_new_context`, в браузере которого контекст уже был, — перезапуск;
- с провайдером эндпоинтов запуск — это `provider.start()` → `driver.attach()`, а
  `provider.stop()` зовётся ровно один раз на эндпоинт, после закрытия его браузера — что бы ни
  случилось: не подключился, упал хук, закрыт, добит, перезапущен под другого владельца;
- identity с профилем на диске (`StatePolicy.user_data_dir`) получает свой браузер, запущенный с
  этим профилем и прокси, и готовый контекст профиля. Профиль занят файловым замком от запуска
  до выхода процесса браузера; образец профиля копируется под тем же замком. Слот, где жил
  профиль, под другую identity перезапускается — и наоборот.

Каждая операция драйвера ограничена своим таймаутом из `Timeouts`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from browser_pool._core.capabilities import uses_profile
from browser_pool._core.tabs import TabPool
from browser_pool.clock import monotonic
from browser_pool.errors import (
    IdentityBusyError,
    PoolInvariantError,
    ProxyFailedError,
    StaleLeaseError,
)
from browser_pool.geometry import Geolocation
from browser_pool.locks import FileLock
from browser_pool.provider import EndpointRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable
    from pathlib import Path

    from browser_pool._core.capabilities import Owner
    from browser_pool._core.proxies import ProxyBinding, ProxyBroker
    from browser_pool._core.scheduler import Grant
    from browser_pool._core.sessions import Sessions
    from browser_pool.config import Limits, Timeouts, Topology
    from browser_pool.driver import ContextSpec, Driver, DriverCapabilities, Endpoint, LaunchSpec
    from browser_pool.hooks import HookRunner
    from browser_pool.identity import Identity
    from browser_pool.locks import IdentityLock
    from browser_pool.procguard import ProcessGuard
    from browser_pool.provider import EndpointProvider
    from browser_pool.proxies import Proxy, ProxyChecker
    from browser_pool.state import Cookie, SessionState

_logger = logging.getLogger(__name__)


@dataclass(eq=False, slots=True)
class _Browser[B]:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    room: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Освобождение места — по одному: иначе две аренды закроют одну и ту же вкладку."""
    browser: B | None = None
    proxy: Proxy | None = None
    """С каким прокси запущен — у драйверов с `proxy_scope="browser"`."""
    owner: Hashable = None
    """Чьи контексты в нём открывались после запуска."""
    fresh: bool = True
    """После запуска контекстов в нём не открывали."""
    profile: Path | None = None
    """С каким профилем на диске запущен."""
    profile_lock: FileLock | None = None
    """Замок профиля — до выхода процесса браузера."""


@dataclass(eq=False, slots=True)
class _Context[C, P]:
    generation: int
    browser_id: str
    context: C
    tabs: TabPool[C, P]
    proxy: ProxyBinding | None = None


class PhysicalResources[B, C, P]:
    """Браузеры, контексты и вкладки, которые стоят за арендами планировщика."""

    def __init__(
        self,
        driver: Driver[B, C, P],
        *,
        topology: Topology,
        timeouts: Timeouts,
        launch_spec: Callable[[str], LaunchSpec],
        context_spec: Callable[[Identity], ContextSpec],
        limits: Limits | None = None,
        on_launch: Callable[[str, B], None] | None = None,
        hooks: HookRunner[B, C, P] | None = None,
        sessions: Sessions[C, P] | None = None,
        proxies: ProxyBroker | None = None,
        is_proxy_fault: Callable[[BaseException], bool] | None = None,
        guard: ProcessGuard | None = None,
        identity_lock: IdentityLock | None = None,
        owner: Owner | None = None,
        on_close: Callable[[str], None] | None = None,
        provider: EndpointProvider | None = None,
        capabilities: DriverCapabilities | None = None,
        geo: ProxyChecker | None = None,
    ) -> None:
        self._driver = driver
        self._topology = topology
        self._timeouts = timeouts
        self._launch_spec = launch_spec
        self._context_spec = context_spec
        self._capacity = topology.pages_per_browser
        self._owner = owner
        self._on_close = on_close
        self._provider = provider
        self._geo = geo
        self._capabilities = capabilities if capabilities is not None else driver.capabilities
        """Возможности пары «драйвер + провайдер» — по ним, а не по драйверу, решает пул."""
        self._endpoints: dict[str, Endpoint] = {}
        """Эндпоинт запущенного браузера слота: его `stop` — после закрытия браузера."""
        self._browsers: dict[str, _Browser[B]] = {}
        self._contexts: dict[str, _Context[C, P]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._latest: dict[str, int] = {}
        self._failed_opens: dict[str, tuple[int, Exception]] = {}
        """Сбой открытия контекста поколения: остальные аренды поколения получают его же."""
        self._close_failures = 0
        self._on_launch = on_launch
        self._hooks = hooks
        self._sessions = sessions
        self._proxies = proxies
        self._proxy_fault = is_proxy_fault
        self._guard = guard
        self._identity_lock = identity_lock
        self._spawn_delay = limits.spawn_delay if limits is not None else 0.0
        self._launches = limits.concurrent_launches if limits is not None else 1
        self._launch_gate = asyncio.Semaphore(self._launches)
        self._next_launch_at = 0.0

    @property
    def close_failures(self) -> int:
        """Сколько вкладок, контекстов и браузеров не закрылось штатно."""
        return self._close_failures + sum(
            physical.tabs.close_failures for physical in self._contexts.values()
        )

    def reconfigure(
        self, *, topology: Topology | None = None, limits: Limits | None = None
    ) -> None:
        """Новые лимиты — для следующих операций; начатые запуски доработают со старыми."""
        if topology is not None:
            self._topology = topology
            self._capacity = topology.pages_per_browser
        if limits is not None:
            self._spawn_delay = limits.spawn_delay
            if limits.concurrent_launches != self._launches:
                self._launches = limits.concurrent_launches
                self._launch_gate = asyncio.Semaphore(limits.concurrent_launches)

    def forget_browser(self, browser_id: str) -> None:
        """Слот браузера убран из пула: забыть его запись. Браузер к этому моменту закрыт."""
        record = self._browsers.pop(browser_id, None)
        if record is not None and record.browser is not None:
            msg = f"Слот {browser_id} забыт, а браузер в нём ещё запущен"
            raise PoolInvariantError(msg)

    @property
    def remembered_identities(self) -> frozenset[str]:
        """Ключи identity, по которым физический слой что-то хранит: контекст, замок, поколение, сбой открытия."""
        return frozenset({*self._contexts, *self._locks, *self._latest, *self._failed_opens})

    def forget_identity(self, key: str) -> None:
        """У identity не осталось контекста ни в учёте, ни физически: забыть всё, что хранилось по её ключу."""
        if key in self._contexts:
            return
        self._latest.pop(key, None)
        self._failed_opens.pop(key, None)
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            del self._locks[key]

    def locate(self, key: str) -> tuple[str, B] | None:
        """Браузер, в котором открыт контекст identity: номер в пуле и нативный объект."""
        physical = self._contexts.get(key)
        if physical is None:
            return None
        browser = self.launched(physical.browser_id)
        return (physical.browser_id, browser) if browser is not None else None

    def launched(self, browser_id: str) -> B | None:
        """Запущенный браузер пула, если он запущен."""
        record = self._browsers.get(browser_id)
        return record.browser if record is not None else None

    async def ensure_browser(self, browser_id: str) -> B | None:
        """Запустить браузер впрок, если он ещё не запущен, и отдать его.

        Браузер владельца от провайдера (профиль антидетекта) впрок не запускается — `None`:
        без identity провайдеру нечего поднимать, браузер запустится под первый контекст.
        """
        if self._owner is not None and self._provider is not None:
            return None
        return await self._ensure_browser(browser_id)

    def has_context(self, key: str, *, generation: int) -> bool:
        """Открыт ли физический контекст identity этого поколения."""
        physical = self._contexts.get(key)
        return physical is not None and physical.generation == generation

    def open_contexts(self) -> list[tuple[str, int]]:
        """Открытые физические контексты: ключ identity и поколение."""
        return [(key, physical.generation) for key, physical in self._contexts.items()]

    # --- аренды ------------------------------------------------------------------------

    async def acquire_page(self, grant: Grant) -> P:
        """Настоящая вкладка под выданную аренду: браузер, контекст и место — по необходимости."""
        physical = await self._ensure_context(grant)
        await self._make_room(grant.browser_id, physical)
        return await physical.tabs.take()

    async def acquire_context(self, grant: Grant) -> None:
        """Открытый контекст под аренду контекста целиком: браузер и контекст — по необходимости."""
        await self._ensure_context(grant)

    def natives(self, grant: Grant) -> tuple[B, C]:
        """Нативные браузер и контекст SDK за арендой — для арендатора."""
        physical = self._current(grant)
        browser = self._browsers[grant.browser_id].browser
        if browser is None:
            msg = f"Браузер {grant.browser_id} закрыт, а аренда {grant.lease_id} ещё жива"
            raise PoolInvariantError(msg)
        return browser, physical.context

    def session(self, grant: Grant) -> object:
        """Объект сессии контекста аренды."""
        self._current(grant)
        return self._sessions.session_of(grant.identity.key) if self._sessions is not None else None

    async def export_state(self, key: str) -> SessionState | None:
        """Состояние живого контекста identity; контекста нет или он мёртв — `None`."""
        physical = self._contexts.get(key)
        if physical is None or self._sessions is None:
            return None
        return await self._sessions.export(key, physical.context)

    async def save_due_states(self, *, interval: float) -> None:
        """Периодическое сохранение состояния живых контекстов."""
        if self._sessions is not None:
            contexts = {key: physical.context for key, physical in self._contexts.items()}
            await self._sessions.save_due(contexts, interval=interval)

    def proxy_of(self, key: str) -> Proxy | None:
        """Прокси открытого контекста identity; нет контекста или прокси — `None`."""
        physical = self._contexts.get(key)
        return physical.proxy.proxy if physical is not None and physical.proxy is not None else None

    def proxy(self, grant: Grant) -> Proxy | None:
        """Прокси контекста аренды; `None` — напрямую или у вендора."""
        binding = self._current(grant).proxy
        return binding.proxy if binding is not None else None

    async def cookies(self, grant: Grant, *, domain: str | None) -> tuple[Cookie, ...]:
        """Куки контекста аренды — для HTTP-клиента той же сессии; `domain` — только видимые ему."""
        physical = self._current(grant)
        async with asyncio.timeout(self._timeouts.state_export):
            state = await self._driver.export_state(physical.context)
        if domain is None:
            return state.cookies
        return tuple(cookie for cookie in state.cookies if _visible(cookie.domain, domain))

    async def proxy_failed(self, key: str, *, generation: int, reason: str) -> None:
        """Прокси подвёл уже в работе: отчёт источнику. Контекст выводит из работы пул. Не бросает."""
        physical = self._contexts.get(key)
        if physical is None or physical.generation != generation or physical.proxy is None:
            return
        if self._proxies is not None:
            await self._proxies.failed(physical.proxy, reason=reason, retrying=False)

    def attachments(self, grant: Grant, page: P) -> dict[str, object]:
        """Вложения вкладки аренды."""
        return self._current(grant).tabs.attachments(page)

    async def release_page(self, grant: Grant, page: P, *, discard: bool) -> None:
        """Вернуть вкладку: тёплой в запас или (`discard`) закрыть — её состояние неизвестно."""
        physical = self._contexts.get(grant.identity.key)
        if physical is None or physical.generation != grant.generation:
            await self._close_stray_page(page)
            return
        if discard:
            await physical.tabs.discard(page)
        else:
            await physical.tabs.give_back(page)

    async def expire_idle_pages(self, *, idle_ttl: float) -> int:
        """Закрыть тёплые вкладки, простаивающие дольше `idle_ttl`, во всех контекстах."""
        closed = 0
        for physical in tuple(self._contexts.values()):
            closed += await physical.tabs.expire_idle(idle_ttl=idle_ttl)
        return closed

    # --- закрытие ----------------------------------------------------------------------

    async def close_context(self, key: str, *, generation: int) -> None:
        """Закрыть физический контекст этого поколения. Не бросает."""
        async with self._lock(key):
            failed = self._failed_opens.get(key)
            if failed is not None and failed[0] <= generation:
                del self._failed_opens[key]  # поколение закрыто: его аренды уже вернулись
            physical = self._contexts.get(key)
            if physical is None or physical.generation != generation:
                return
            await self._close_physical(key, physical)

    async def close_browser(self, browser_id: str) -> None:
        """Закрыть браузер и все его контексты. Не закрылся штатно — добить. Не бросает.

        Контексты закрываются одновременно: время остановки не растёт с их числом, а зависший
        контекст стоит одного таймаута, а не таймаута на каждый.
        """
        await asyncio.gather(
            *(
                self.close_context(key, generation=physical.generation)
                for key, physical in tuple(self._contexts.items())
                if physical.browser_id == browser_id
            )
        )
        record = self._browsers.get(browser_id)
        if record is None:
            return
        async with record.lock:
            browser, record.browser = record.browser, None
            if browser is not None:
                try:
                    await self._close_browser(browser_id, browser)
                finally:
                    _free_profile(record)

    async def aclose(self) -> None:
        """Закрыть всё: контексты, затем браузеры. Браузеры — одновременно, каждый со своими контекстами."""
        await asyncio.gather(
            *(self.close_browser(browser_id) for browser_id in tuple(self._browsers))
        )

    # --- внутреннее --------------------------------------------------------------------

    async def _ensure_browser(self, browser_id: str, *, shared: bool = False) -> B:
        """Браузер слота; `shared` — под общий контекст: браузер чужого профиля перезапускается."""
        record = self._browsers.setdefault(browser_id, _Browser[B]())
        async with record.lock:
            if shared and record.browser is not None and record.profile is not None:
                await self._relaunching(browser_id, record)
            if record.browser is None:
                return await self._start_into(record, browser_id, proxy=None)
            return record.browser

    async def _browser_for(self, grant: Grant, proxy: Proxy | None) -> B:
        """Браузер владельца identity с нужным прокси: годный — как есть, негодный — перезапуск."""
        owner = self._owner(grant.identity) if self._owner is not None else None
        browser_id = grant.browser_id
        record = self._browsers.setdefault(browser_id, _Browser[B]())
        async with record.lock:
            browser = record.browser
            if browser is not None and not self._reusable(
                record, owner, proxy, profile=_profile(grant.identity)
            ):
                await self._relaunching(browser_id, record)
                browser = None
            if browser is None:
                browser = await self._start_into(
                    record, browser_id, proxy=proxy, identity=grant.identity
                )
            record.owner, record.fresh = owner, False
            return browser

    def _reusable(
        self, record: _Browser[B], owner: Hashable, proxy: Proxy | None, *, profile: Path | None
    ) -> bool:
        capabilities = self._capabilities
        if record.profile != profile:
            return False
        at_launch = capabilities.proxy_scope == "browser" or profile is not None
        if at_launch and record.proxy != proxy:
            return False
        return record.fresh or (capabilities.can_new_context and record.owner == owner)

    async def _relaunching(self, browser_id: str, record: _Browser[B]) -> None:
        """Закрыть браузер под перезапуск. Контексты в нём — нарушение: планировщик не пустил бы."""
        inside = [
            key for key, physical in self._contexts.items() if physical.browser_id == browser_id
        ]
        if inside:
            msg = f"Браузер {browser_id} перезапускается под другого владельца, а в нём {inside}"
            raise PoolInvariantError(msg)
        browser, record.browser = record.browser, None
        if browser is None:
            return
        _logger.info("Браузер %s перезапускается под нового владельца", browser_id)
        if self._on_close is not None:
            self._on_close(browser_id)
        try:
            await self._close_browser(browser_id, browser)
        finally:
            _free_profile(record)

    async def _start_into(
        self,
        record: _Browser[B],
        browser_id: str,
        *,
        proxy: Proxy | None,
        identity: Identity | None = None,
    ) -> B:
        profile = _profile(identity) if identity is not None else None
        lock = await self._take_profile(identity) if identity is not None and profile else None
        try:
            browser = await self._start(browser_id, proxy=proxy, identity=identity, profile=profile)
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        record.browser, record.proxy, record.owner, record.fresh = browser, proxy, None, True
        record.profile, record.profile_lock = profile, lock
        if self._on_launch is not None:
            self._on_launch(browser_id, browser)
        return browser

    async def _take_profile(self, identity: Identity) -> FileLock:
        """Занять профиль identity; ждать не дольше `Timeouts.open`. Образец — под замком."""
        policy = identity.state
        directory = policy.user_data_dir
        if directory is None:
            msg = f"identity {identity.key} без профиля на диске"
            raise PoolInvariantError(msg)
        lock = FileLock(directory.with_name(f"{directory.name}.lock"))
        try:
            async with asyncio.timeout(self._timeouts.open):
                await lock.acquire()
        except TimeoutError as error:
            _logger.warning("Профиль identity %s занят другим браузером", identity.key)
            raise IdentityBusyError(identity=identity.key) from error
        try:
            if policy.profile_template is not None:
                await asyncio.to_thread(_clone_profile, policy.profile_template, directory)
        except BaseException:
            lock.release()
            raise
        return lock

    def persistent(self, browser_id: str) -> bool:
        """Запущен ли браузер слота с профилем на диске."""
        record = self._browsers.get(browser_id)
        return record is not None and record.profile is not None

    async def _start(
        self,
        browser_id: str,
        *,
        proxy: Proxy | None,
        identity: Identity | None,
        profile: Path | None = None,
    ) -> B:
        """Запуск и хуки `after_browser_started`; упавший хук не оставляет живой браузер без хозяина."""
        browser = await self._launch(browser_id, proxy=proxy, identity=identity, profile=profile)
        try:
            pid = self._driver.pid(browser)
            if self._guard is not None and pid is not None:
                await self._guard.track(pid)
            if self._hooks is not None:
                await self._hooks.run_after_browser_started(browser, browser_id)
        except BaseException:
            await self._close_browser(browser_id, browser)
            raise
        return browser

    async def _launch(
        self,
        browser_id: str,
        *,
        proxy: Proxy | None,
        identity: Identity | None,
        profile: Path | None = None,
    ) -> B:
        """Запуск под общим ограничением: не больше `concurrent_launches` разом, с паузой между стартами."""
        async with self._launch_gate:
            wait = self._next_launch_at - monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_launch_at = monotonic() + self._spawn_delay
            spec = self._launch_spec(browser_id)
            spec.proxy = proxy
            spec.user_data_dir = profile
            if self._hooks is not None:
                await self._hooks.run_before_launch(spec, browser_id)
            async with asyncio.timeout(self._timeouts.startup):
                if self._provider is None:
                    return await self._driver.launch(spec)
                return await self._attach(
                    self._provider,
                    EndpointRequest(browser_id=browser_id, identity=identity, spec=spec),
                )

    async def _attach(self, provider: EndpointProvider, request: EndpointRequest) -> B:
        """Браузер от провайдера; не подключились — эндпоинт сразу освобождается."""
        endpoint = await provider.start(request)
        try:
            browser = await self._driver.attach(endpoint)
        except BaseException:
            await self._stop_endpoint(endpoint)
            raise
        self._endpoints[request.browser_id] = endpoint
        return browser

    async def _stop_endpoint(self, endpoint: Endpoint) -> None:
        """Освободить эндпоинт у провайдера. Не бросает: сбой учитывается в `close_failures`."""
        if self._provider is None:
            return
        try:
            async with asyncio.timeout(self._timeouts.close):
                await self._provider.stop(endpoint)
        except Exception:
            self._close_failures += 1
            _logger.warning("Провайдер не освободил эндпоинт %s", endpoint.kind, exc_info=True)

    async def _ensure_context(self, grant: Grant) -> _Context[C, P]:
        key = grant.identity.key
        latest = self._latest.get(key, 0)
        if grant.generation < latest:
            raise StaleLeaseError(
                lease_id=grant.lease_id, generation=grant.generation, current=latest
            )
        async with self._lock(key):
            physical = self._contexts.get(key)
            if physical is not None and _serves(physical, grant):
                return physical
            failed = self._failed_opens.get(key)
            if failed is not None and failed[0] == grant.generation:
                # Аренду выдали до сбоя открытия: второй раз подряд не запускаем и не входим.
                raise failed[1]
            try:
                return await self._open_for(grant, physical)
            except Exception as error:
                self._failed_opens[key] = (grant.generation, error)
                raise

    async def _open_for(self, grant: Grant, stale: _Context[C, P] | None) -> _Context[C, P]:
        """Браузер и контекст под аренду; `stale` — контекст прежнего поколения, он закрывается."""
        # С владельцем браузер запускается под прокси контекста — при открытии, не заранее.
        owned = self._owner is not None and self._owner(grant.identity) is not None
        browser = (
            None
            if owned
            else await self._ensure_browser(grant.browser_id, shared=self._owner is not None)
        )
        if stale is not None:
            await self._close_physical(grant.identity.key, stale)
        return await self._open_context(grant, browser)

    async def _open_context(self, grant: Grant, browser: B | None) -> _Context[C, P]:
        """Замок identity — на всю жизнь контекста; открытие не удалось — замок отпускается."""
        key = grant.identity.key
        await self._take_identity(key)
        try:
            return await self._open_with_proxies(grant, browser)
        except BaseException:
            await self._free_identity(key)
            raise

    async def _take_identity(self, key: str) -> None:
        if self._identity_lock is None:
            return
        try:
            async with asyncio.timeout(self._timeouts.open):
                await self._identity_lock.acquire(key)
        except TimeoutError as error:
            raise IdentityBusyError(identity=key) from error

    async def _free_identity(self, key: str) -> None:
        if self._identity_lock is None:
            return
        try:
            await self._identity_lock.release(key)
        except Exception:
            _logger.warning("Замок identity %s не отпустился", key, exc_info=True)

    async def _open_with_proxies(self, grant: Grant, browser: B | None) -> _Context[C, P]:
        """Контекст identity; прокси не пропустил — ещё раз с другим, пока позволяет политика."""
        identity, excluded, attempt = grant.identity, set[str](), 0
        while True:
            state = await self._sessions.restore(identity) if self._sessions is not None else None
            binding = await self._bind_proxy(identity, frozenset(excluded))
            try:
                return await self._open_attempt(grant, browser, state=state, binding=binding)
            except BaseException as error:
                await self._release_proxy(binding)
                if binding is None or self._proxies is None or not self._is_proxy_fault(error):
                    raise
                attempt += 1
                await self._proxy_failed_on_open(
                    identity, binding=binding, proxies=self._proxies, error=error, attempt=attempt
                )
                excluded.add(binding.lease.proxy_id)

    async def _proxy_failed_on_open(
        self,
        identity: Identity,
        *,
        binding: ProxyBinding,
        proxies: ProxyBroker,
        error: BaseException,
        attempt: int,
    ) -> None:
        """Прокси не пропустил открытие: отчёт и событие; повтора не будет — `ProxyFailedError`."""
        proxy = binding.proxy
        if proxy is None:
            raise error
        retrying = proxies.can_retry(identity, attempt)
        reason = type(error).__name__
        await proxies.failed(binding, reason=reason, retrying=retrying)
        if not retrying:
            raise ProxyFailedError(proxy=proxy, identity=identity.key, reason=reason) from error

    async def _bind_proxy(
        self, identity: Identity, excluded: frozenset[str]
    ) -> ProxyBinding | None:
        if self._proxies is None:
            return None
        preferred = (
            self._sessions.pending_proxy_ref(identity.key) if self._sessions is not None else None
        )
        try:
            return await self._proxies.bind(identity, preferred=preferred, exclude=excluded)
        except BaseException:
            if self._sessions is not None:
                self._sessions.forget_pending(identity.key)
            raise

    async def _release_proxy(self, binding: ProxyBinding | None) -> None:
        if binding is not None and self._proxies is not None:
            await self._proxies.release(binding)

    async def _open_attempt(
        self,
        grant: Grant,
        browser: B | None,
        *,
        state: SessionState | None,
        binding: ProxyBinding | None,
    ) -> _Context[C, P]:
        """Одна попытка: браузер владельца, контекст с прокси и состоянием, хуки, сессия, вкладки.

        `browser` — `None` у браузера с владельцем: он запускается здесь, с прокси контекста.
        """
        identity = grant.identity
        spec = self._context_spec(identity)
        spec.state = state
        spec.reuse_default = not self._capabilities.can_new_context or uses_profile(identity)
        proxy = binding.proxy if binding is not None else None
        if proxy is not None and self._geo is not None and not spec.reuse_default:
            await self._match_proxy_geo(spec, proxy)
        owned = browser is None
        if browser is None:
            browser = await self._owned_browser(grant, proxy)
        else:
            spec.proxy = proxy
        proxy_ref = binding.lease.proxy_id if binding is not None and proxy is not None else None
        gate = (
            self._proxies.gate(binding)
            if self._proxies is not None and binding is not None
            else contextlib.nullcontext()
        )
        async with gate:
            context = await self._create_context(identity, browser, spec)
            try:
                if self._hooks is not None:
                    await self._hooks.run_after_context_created(context, identity)
                if self._sessions is not None:
                    await self._sessions.open(
                        identity,
                        context,
                        restored=spec.state,
                        proxy=proxy if owned else spec.proxy,
                        proxy_ref=proxy_ref,
                    )
            except BaseException:
                await self._discard_context(identity.key, context)
                raise
        prepare, reset = self._page_callbacks(identity)
        physical = _Context(
            generation=grant.generation,
            browser_id=grant.browser_id,
            context=context,
            tabs=TabPool(
                self._driver,
                context,
                prepare=prepare,
                reset=reset,
                warm_limit=self._topology.warm_pages_per_identity,
                timeouts=self._timeouts,
            ),
            proxy=binding,
        )
        # Сначала учёт, потом отчёт источнику: отмена на отчёте не оставит открытый контекст ничьим.
        self._contexts[identity.key] = physical
        self._latest[identity.key] = grant.generation
        if binding is not None and self._proxies is not None:
            await self._proxies.succeeded(binding)
        return physical

    async def _match_proxy_geo(self, spec: ContextSpec, proxy: Proxy) -> None:
        """Часовой пояс, локаль и геопозиция — по выходу прокси, если identity не задала свои."""
        if spec.timezone is not None and spec.locale is not None and spec.geolocation is not None:
            return
        assert self._geo is not None  # noqa: S101 — зовётся только с проверкой гео
        try:
            async with asyncio.timeout(self._timeouts.context_create):
                geo = await self._geo.check(proxy)
        except Exception as error:  # noqa: BLE001 — без гео контекст всё равно откроется
            _logger.warning("Гео прокси %s не определилось (%s)", proxy.label, type(error).__name__)
            return
        if geo is None:
            return
        applies = (
            self._capabilities.context_settings
        )  # чего драйвер не применяет, то и не подбираем
        if spec.timezone is None and "timezone" in applies:
            spec.timezone = geo.timezone
        if spec.locale is None and "locale" in applies:
            spec.locale = geo.locale
        if spec.geolocation is not None or "geolocation" not in applies:
            return
        if geo.latitude is not None and geo.longitude is not None:
            spec.geolocation = Geolocation(latitude=geo.latitude, longitude=geo.longitude)

    async def _owned_browser(self, grant: Grant, proxy: Proxy | None) -> B:
        """Браузер под контекст; не запустился — прокси, выбранный для записи identity, забыт."""
        try:
            return await self._browser_for(grant, proxy)
        except BaseException:
            if self._sessions is not None:
                self._sessions.forget_pending(grant.identity.key)
            raise

    async def _create_context(self, identity: Identity, browser: B, spec: ContextSpec) -> C:
        try:
            if self._hooks is not None:
                await self._hooks.run_before_context(spec, identity)
            async with asyncio.timeout(self._timeouts.context_create):
                return await self._driver.new_context(browser, spec)
        except BaseException:
            if self._sessions is not None:
                self._sessions.forget_pending(identity.key)
            raise

    def _page_callbacks(
        self, identity: Identity
    ) -> tuple[Callable[[P], Awaitable[None]], Callable[[P], Awaitable[bool]]]:
        """Прогрев новой вкладки (хуки `after_page_created`, затем flow) и её сброс перед возвратом."""
        hooks, sessions = self._hooks, self._sessions

        async def prepare(page: P) -> None:
            if hooks is not None:
                await hooks.run_after_page_created(page, identity)
            if sessions is not None:
                await sessions.prepare_page(identity.key, page)

        async def reset(page: P) -> bool:
            return await sessions.reset_page(identity.key, page) if sessions is not None else True

        return prepare, reset

    async def _make_room(self, browser_id: str, physical: _Context[C, P]) -> None:
        """Браузер полон открытых вкладок, а своей тёплой нет — закрыть самую холодную у соседа."""
        if physical.tabs.idle:
            return
        async with self._browsers[browser_id].room:
            while self._open_pages(browser_id) >= self._capacity:
                victim = self._coldest_idle(browser_id)
                if victim is None or not await victim.tabs.trim_idle():
                    return

    def _open_pages(self, browser_id: str) -> int:
        return sum(
            physical.tabs.open
            for physical in self._contexts.values()
            if physical.browser_id == browser_id
        )

    def _coldest_idle(self, browser_id: str) -> _Context[C, P] | None:
        candidates = [
            physical
            for physical in self._contexts.values()
            if physical.browser_id == browser_id and physical.tabs.coldest_idle_since is not None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda physical: physical.tabs.coldest_idle_since or 0.0)

    async def _close_physical(self, key: str, physical: _Context[C, P]) -> None:
        await physical.tabs.aclose()
        if self._sessions is not None:
            await self._sessions.close(key, physical.context)
        self._close_failures += physical.tabs.close_failures
        self._contexts.pop(key, None)
        try:
            async with asyncio.timeout(self._timeouts.close):
                await self._driver.close_context(physical.context)
        except Exception:
            self._close_failures += 1
            _logger.warning("Контекст %s не закрылся штатно", key, exc_info=True)
        await self._release_proxy(physical.proxy)
        await self._free_identity(key)

    async def _discard_context(self, key: str, context: C) -> None:
        try:
            async with asyncio.timeout(self._timeouts.close):
                await self._driver.close_context(context)
        except Exception:
            self._close_failures += 1
            _logger.warning("Контекст %s не закрылся после сбоя открытия", key, exc_info=True)

    async def _close_browser(self, browser_id: str, browser: B) -> None:
        """Закрыть; не вышло — добить драйвером; процесс всё ещё жив — страж убивает дерево."""
        pid = self._driver.pid(browser)
        try:
            await self._close_or_kill(browser_id, browser)
        finally:
            if self._guard is not None and pid is not None:
                await self._guard.release(pid, grace=self._timeouts.kill)
            endpoint = self._endpoints.pop(browser_id, None)
            if endpoint is not None:
                await self._stop_endpoint(endpoint)

    async def _close_or_kill(self, browser_id: str, browser: B) -> None:
        try:
            async with asyncio.timeout(self._timeouts.close):
                await self._driver.close_browser(browser)
        except Exception:
            self._close_failures += 1
            _logger.warning("Браузер %s не закрылся штатно, добиваю", browser_id, exc_info=True)
        else:
            return
        try:
            async with asyncio.timeout(self._timeouts.kill):
                await self._driver.kill_browser(browser)
        except Exception:
            _logger.exception("Браузер %s не удалось добить", browser_id)

    async def _close_stray_page(self, page: P) -> None:
        """Вкладка контекста, которого уже нет: закрыть, не считая ничьей."""
        try:
            async with asyncio.timeout(self._timeouts.close):
                await self._driver.close_page(page)
        except Exception:
            self._close_failures += 1
            _logger.warning("Вкладка без контекста не закрылась", exc_info=True)

    def _current(self, grant: Grant) -> _Context[C, P]:
        physical = self._contexts.get(grant.identity.key)
        if physical is None or physical.generation != grant.generation:
            raise StaleLeaseError(
                lease_id=grant.lease_id,
                generation=grant.generation,
                current=physical.generation if physical is not None else 0,
            )
        return physical

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _is_proxy_fault(self, error: BaseException) -> bool:
        return self._proxy_fault is not None and self._proxy_fault(error)


def _profile(identity: Identity) -> Path | None:
    return identity.state.user_data_dir if uses_profile(identity) else None


def _free_profile[B](record: _Browser[B]) -> None:
    """Браузер закрыт: профиль свободен."""
    lock, record.profile_lock, record.profile = record.profile_lock, None, None
    if lock is not None:
        lock.release()


def _clone_profile(template: Path, directory: Path) -> None:
    """Скопировать образец в профиль, которого ещё нет: каталог отсутствует или пуст."""
    if directory.is_dir() and any(directory.iterdir()):
        return
    shutil.copytree(template, directory, dirs_exist_ok=True)


def _visible(cookie_domain: str, domain: str) -> bool:
    """Отправит ли браузер куку домена `cookie_domain` на хост `domain`."""
    owner, host = cookie_domain.lstrip(".").lower(), domain.lower()
    return host == owner or host.endswith("." + owner)


def _serves[C, P](physical: _Context[C, P], grant: Grant) -> bool:
    """Годится ли контекст аренде: то же поколение — да; старее — заменить; новее — аренда устарела."""
    if physical.generation > grant.generation:
        raise StaleLeaseError(
            lease_id=grant.lease_id, generation=grant.generation, current=physical.generation
        )
    if physical.generation < grant.generation:
        return False
    if physical.browser_id != grant.browser_id:
        msg = f"Контекст {grant.identity.key} в {physical.browser_id}, а аренда — в {grant.browser_id}"
        raise PoolInvariantError(msg)
    return True


__all__ = ["PhysicalResources"]
