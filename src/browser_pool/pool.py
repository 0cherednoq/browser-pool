"""Фасад пула: `BrowserPool` и аренда вкладки.

Связывает планировщик (кому что выдать) и физические ресурсы (что за этим стоит) и отдаёт
арендатору нативные объекты SDK::

    async with BrowserPool(driver, config=config) as pool:
        async with pool.page(identity) as lease:
            await site.send(lease.page, message)

Планировщик синхронный, поэтому все решения принимаются между `await` и гонок в учёте нет.
После каждого изменения фасад раздаёт освободившееся ожидающим и отдаёт выведенные из работы
контексты на закрытие фоновой задаче — фоновые задачи принадлежат пулу и завершаются в `stop()`.

Исключение арендатора пробрасывается наружу как есть: пул только реагирует на него. Что
сломалось, решает конвейер: `lease.report` → вид, объявленный самим исключением или его причиной
(`PoolSignal`, `pool_error_kind`) → `classifier` пула → `classify` драйвера → `unknown`. Каждый шаг
идёт по цепочке причин снаружи внутрь (`error_chain`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal, Self, cast, overload

from browser_pool._core.assembly import CoreCallbacks, build_core
from browser_pool._core.capabilities import (
    browser_owner,
    effective_config,
    pair_capabilities,
    unmet,
)
from browser_pool._core.facade_support import (
    LeaseLink,
    Natives,
    candidates_of,
    first_failure,
    first_key,
    key_of,
    new_slots,
    removal_order,
)
from browser_pool._core.outcomes import Outcomes, Verdicts
from browser_pool._core.retry import RETRIED_KINDS, run_with_retries
from browser_pool._core.threads import bind_threads
from browser_pool.clock import monotonic
from browser_pool.config import Backoff, Lifecycle, Limits, PoolConfig, Recovery, Recycling
from browser_pool.debug import HeldPages
from browser_pool.driver import PageLabeler, driver_problems
from browser_pool.errors import (
    AcquireTimeoutError,
    Classifier,
    ConfigError,
    ErrorKind,
    IdentityBlockedError,
    IdentityCoolingDownError,
    PoolInvariantError,
    PoolStoppedError,
    PoolUnavailableError,
    StartupTimeoutError,
    UnsupportedRequirementError,
)
from browser_pool.events import (
    AcquireWatchdog,
    ContextClosed,
    ContextOpened,
    ContextRetired,
    IdentityBlocked,
    IdentityCooledDown,
    LeaseAcquired,
    OrphansReaped,
    PoolHealth,
)
from browser_pool.hooks import HookRunner, Hooks
from browser_pool.lease import ContextLease, PageLease
from browser_pool.windows.manager import WindowManager

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        Awaitable,
        Callable,
        Collection,
        Coroutine,
        Iterable,
        Sequence,
    )
    from types import TracebackType

    from browser_pool._core.scheduler import (
        ContextView,
        Grant,
        Waiter,
    )
    from browser_pool.driver import Driver
    from browser_pool.events import Handler, PoolEvent
    from browser_pool.evidence import EvidenceSink
    from browser_pool.flow import SessionFlow
    from browser_pool.host import HostProbe
    from browser_pool.identity import Identity
    from browser_pool.locks import IdentityLock
    from browser_pool.procguard import ProcessGuard
    from browser_pool.provider import EndpointProvider
    from browser_pool.proxies import ProxyChecker, ProxySource
    from browser_pool.snapshot import IdentityStatus, PoolSnapshot
    from browser_pool.state import Cookie, SessionState, StateStore

_logger = logging.getLogger(__name__)


class BrowserPool[B, C, P]:
    """Пул браузеров над драйвером SDK: аренда вкладок identity с контекстами, лимитами и жизненным циклом."""

    def __init__(
        self,
        driver: Driver[B, C, P],
        *,
        config: PoolConfig | None = None,
        flow: SessionFlow[C, P, Any] | None = None,
        state_store: StateStore | None = None,
        proxy_source: ProxySource | None = None,
        classifier: Classifier | None = None,
        plugins: Sequence[object] = (),
        process_guard: ProcessGuard | None = None,
        identity_lock: IdentityLock | None = None,
        provider: EndpointProvider | None = None,
        host_probe: HostProbe | None = None,
        proxy_checker: ProxyChecker | None = None,
        evidence_sink: EvidenceSink | None = None,
    ) -> None:
        problems = driver_problems(driver, attaches=provider is not None)
        if problems:
            # Драйвер, который объявил больше, чем умеет, сломался бы посреди работы — молча.
            raise ConfigError(*problems)
        if driver.capabilities.thread_affinity:
            # Синхронный SDK: у каждого браузера свой поток, все вызовы драйвера — в нём.
            driver = bind_threads(driver)
        self._driver = driver
        self._hook_runner: HookRunner[B, C, P] = HookRunner()
        self._control = LeaseLink(
            driver,
            attachments=self._attachments_of,
            retire=self._retire_context,
            cookies=self._cookies_of,
        )
        self.hooks: Hooks[B, C, P] = self._hook_runner
        """Хуки пула: `@pool.hooks.before_context` и остальные точки."""
        for plugin in plugins:
            self.hooks.add(plugin)
        self._requested = config if config is not None else PoolConfig()
        self._capabilities = pair_capabilities(driver.capabilities, provider)
        self._config = effective_config(self._requested, self._capabilities)
        owner = browser_owner(self._capabilities, proxy_source=proxy_source is not None)
        self._verdicts = Verdicts(driver, classifier)
        self._provider = provider
        self._has_proxy_source = proxy_source is not None
        self._direct_sticky_warned = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._resizing = asyncio.Lock()
        core = build_core(
            driver=driver,
            config=self._config,
            capabilities=self._capabilities,
            owner=owner,
            hooks=self._hook_runner,
            callbacks=CoreCallbacks(
                spawn=self._spawn,
                settle=self._settle,
                fail_waiters=self._fail_waiters,
                health_checked=self._health_checked,
                is_proxy_fault=self._verdicts.is_proxy_fault,
                config=lambda: self._config,
                windows_shown=lambda: self.windows.shown,
            ),
            flow=flow,
            state_store=state_store,
            proxy_source=proxy_source,
            process_guard=process_guard,
            identity_lock=identity_lock,
            provider=provider,
            host_probe=host_probe,
            proxy_checker=proxy_checker,
        )
        self._bus = core.bus
        self._store = core.store
        self._guard = core.guard
        self._sessions = core.sessions
        self._scheduler = core.scheduler
        self._proxy_broker = core.proxy_broker
        self._resources = core.resources
        self._resource_guard = core.resource_guard
        self._supervisor = core.supervisor
        self._observer = core.observer
        self.windows: WindowManager[B, P] = WindowManager(
            driver,
            config=self._config.windows,
            locate=self._resources.locate,
            emit=self._bus.emit,
            timeout=self._config.timeouts.ping,
            tabs_per_identity=min(
                self._config.topology.pages_per_identity or self._config.topology.pages_per_browser,
                self._config.topology.pages_per_browser,
            ),
        )
        """Окна для отладки: `retile()`, `focus(key)`, `minimize_idle()`, `snapshot()`."""
        self._attach_windows()
        self.held_pages: HeldPages = HeldPages(self._config.debug.hold_on_error)
        """Отладка: вкладки, задержанные после ошибки (`Debug.hold_on_error`) — `release()`, `held`."""
        if self._config.debug.label_windows and isinstance(driver, PageLabeler):
            self.hooks.after_page_created(self._label_page)
        self._outcomes: Outcomes[P] = Outcomes(
            verdicts=self._verdicts,
            driver=driver,
            scheduler=self._scheduler,
            supervisor=self._supervisor,
            resources=self._resources,
            observer=self._observer,
            evidence=evidence_sink,
            config=lambda: self._config,
            emit=self._bus.emit,
            spawn=self._spawn,
            fail_blocked_waiters=self._fail_blocked_waiters,
            give_back=self._return,
        )
        self._accepting = False
        self._prepared = False
        self._closed = False
        self._futures: dict[Waiter, asyncio.Future[None]] = {}
        self._active: dict[int, tuple[Grant, P | None]] = {}
        self._drained = asyncio.Event()
        self._drained.set()
        self._wake: asyncio.TimerHandle | None = None

    def _attach_windows(self) -> None:
        """Менеджер окон слушает вкладки, аренды и закрытия контекстов — если окна включены."""
        windows = self.windows
        if windows.at_launch:
            self.hooks.before_launch(windows.before_launch)
        if not windows.enabled:
            return
        self.hooks.after_page_created(windows.page_opened)
        self.hooks.after_acquire(windows.lease_acquired)
        self.hooks.before_release(windows.lease_released)
        self._bus.on(ContextClosed, lambda event: windows.context_closed(event.key))

    async def _label_page(self, page: P, identity: Identity) -> None:
        """`Debug.label_windows`: чья вкладка и через какой прокси — в заголовке окна."""
        labeler = cast("PageLabeler[P]", self._driver)
        proxy = self._resources.proxy_of(identity.key)
        label = f"[{identity.key} · {proxy.label}] " if proxy is not None else f"[{identity.key}] "
        try:
            async with asyncio.timeout(self._config.timeouts.ping):
                await labeler.label_page(page, label)
        except Exception as error:  # noqa: BLE001 — отладочная подпись не роняет аренду
            _logger.warning(
                "Подпись окна %s не поставилась (%s)", identity.key, type(error).__name__
            )

    @property
    def config(self) -> PoolConfig:
        """Конфиг, с которым пул создан."""
        return self._requested

    @property
    def effective_config(self) -> PoolConfig:
        """Конфиг, который пул исполняет на своём драйвере.

        Отличается от `config` там, где драйвер строже: `pages_per_browser` не больше
        `max_pages_hint`; браузер, который принадлежит одной identity, — `contexts_per_browser=1`.
        """
        return self._config

    # --- жизненный цикл ----------------------------------------------------------------

    async def start(self) -> None:
        """Запустить пул: подготовить драйвер и начать принимать аренды."""
        if self._accepting:
            return
        if self._closed:
            msg = "Остановленный пул не перезапускается — создайте новый"
            raise PoolStoppedError(msg)
        if self._config.resources.active and not self._resource_guard.active:
            _logger.warning(
                "Секция resources игнорируется: нет замеров хоста (pip install browser-pool[resources])"
            )
        for option in self._unapplied_debug_options():
            _logger.warning("Отладочная опция %s игнорируется: драйвер её не применяет", option)
        problem = self.windows.problem()
        if problem is not None:
            _logger.warning("Окна для отладки выключены: %s", problem)
        downgrade = self.windows.downgrade()
        if downgrade is not None:
            _logger.warning("Окна для отладки: %s", downgrade)
        reaped = await self._guard.reap_orphans() + await self._reap_provider_orphans()
        if reaped:
            _logger.warning("Добито браузеров, переживших свой пул: %d", reaped)
            self._bus.emit(OrphansReaped(count=reaped))
        try:
            async with asyncio.timeout(self._config.timeouts.startup):
                await self._driver.prepare()
        except TimeoutError as error:
            raise StartupTimeoutError(timeout=self._config.timeouts.startup) from error
        self._prepared = True
        self._accepting = True
        await self._supervisor.start()

    def _unapplied_debug_options(self) -> list[str]:
        """Отладочные опции конфига, которых драйвер не умеет."""
        debug, applies = self._config.debug, self._capabilities.debug_options
        wanted = {
            "slow_mo": debug.slow_mo is not None,
            "keep_background_active": debug.keep_background_active,
        }
        return [name for name, on in wanted.items() if on and name not in applies]

    async def _reap_provider_orphans(self) -> int:
        """Сироты прошлого запуска у провайдера эндпоинтов. Сбой — предупреждение, не отказ старта."""
        if self._provider is None:
            return 0
        try:
            async with asyncio.timeout(self._config.timeouts.startup):
                return await self._provider.reap_orphans()
        except Exception:
            _logger.warning("Провайдер не прибрал сирот прошлого запуска", exc_info=True)
            return 0

    def on[E: PoolEvent](self, kind: type[E], handler: Handler[E]) -> Callable[[], None]:
        """Подписаться на события типа `kind` (и наследников; `PoolEvent` — на все). Возвращает отписку."""
        return self._bus.on(kind, handler)

    def snapshot(self) -> PoolSnapshot:
        """Согласованный снимок пула без секретов: ёмкость, аренды, очередь, браузеры, контексты."""
        return self._observer.snapshot()

    async def export_state(self, identity: Identity | str) -> SessionState | None:
        """Состояние сессии identity (объект или ключ): живого контекста, а нет его — сохранённое в хранилище."""
        key = key_of(identity)
        live = await self._resources.export_state(key)
        if live is not None:
            return live
        record = await self._store.load(key)
        return record.state if record is not None else None

    # --- статусы identity --------------------------------------------------------------

    def identity_status(self, identity: Identity | str) -> IdentityStatus:
        """Пауза, блокировка и неудачные открытия identity (объект или ключ)."""
        return self._scheduler.identity_status(key_of(identity), now=monotonic())

    def cool_down(self, identity: Identity | str, seconds: float) -> None:
        """Поставить identity на паузу: в `any_of` её пропустят, заявка только на неё подождёт."""
        key = key_of(identity)
        self._scheduler.cool_down(key, until=monotonic() + seconds)
        self._bus.emit(IdentityCooledDown(key=key, seconds=seconds))
        self._settle()

    def block(self, identity: Identity | str, reason: str) -> None:
        """Заблокировать identity до `unblock`: заявки только на неё получат `IdentityBlockedError`."""
        key = key_of(identity)
        self._scheduler.block(key, reason=reason)
        self._bus.emit(IdentityBlocked(key=key, kind=None))
        self._fail_blocked_waiters()
        self._settle()

    def unblock(self, identity: Identity | str) -> None:
        """Снять блокировку и паузу identity."""
        self._scheduler.unblock(key_of(identity))
        self._settle()

    async def stop(self, *, drain_timeout: float | None = None) -> None:
        """Остановить пул: не принимать аренды, отказать ожидающим, дождаться занятых, закрыть всё."""
        wait = self._config.timeouts.drain if drain_timeout is None else drain_timeout
        await self._shutdown(wait=wait)

    async def terminate(self) -> None:
        """Остановить пул, не дожидаясь занятых аренд."""
        await self._shutdown(wait=0.0)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    # --- аренда ------------------------------------------------------------------------

    @asynccontextmanager
    async def page(
        self,
        identity: Identity | None = None,
        *,
        any_of: Sequence[Identity] | None = None,
        # Не asyncio.timeout снаружи: тот оборвал бы и работу арендатора, а этот ограничивает
        # только ожидание выдачи и называет кандидатов в AcquireTimeoutError.
        acquire_timeout: float | None = None,
        priority: int = 0,
        wait_cooldown: bool = True,
    ) -> AsyncGenerator[PageLease[B, C, P, Any]]:
        """Арендовать вкладку identity — или любой из `any_of`, в порядке предпочтения.

        `acquire_timeout` — сколько ждать выдачи (по умолчанию `Timeouts.acquire`, `None` — пока пул
        жив); `priority` — больше, раньше в очереди; `wait_cooldown=False` — не ждать, если все
        кандидаты на паузе, а сразу получить `IdentityCoolingDownError`.
        """
        candidates = self._feasible(candidates_of(identity, any_of))
        self._check_candidates(candidates, wait_cooldown=wait_cooldown)
        requested = monotonic()
        grant = await self._acquire(candidates, limit=acquire_timeout, priority=priority)
        self._warn_unpinned(grant.identity)
        try:
            page = await self._resources.acquire_page(grant)
            natives = self._natives(grant)
        except BaseException as error:
            self._outcomes.acquire_failed(grant, error)
            raise
        acquired = self._acquired(grant, requested)
        lease = PageLease(
            control=self._control,
            lease_id=grant.lease_id,
            identity=grant.identity,
            browser_id=grant.browser_id,
            generation=grant.generation,
            browser=natives.browser,
            context=natives.context,
            page=page,
            session=natives.session,
            proxy=natives.proxy,
        )
        self._active[grant.lease_id] = (grant, page)
        watch = self._observer.watch(grant)
        error: BaseException | None = None
        try:
            await self._hook_runner.run_after_acquire(lease)
            yield lease
        except BaseException as raised:
            error = raised
            revoked = watch.revoked(raised)
            if revoked is not None:
                raise revoked from raised
            raise
        finally:
            watch.stop()
            await self._detached(
                self._conclude(grant, page, lease=lease, error=error, acquired=acquired)
            )

    @asynccontextmanager
    async def context(
        self,
        identity: Identity | None = None,
        *,
        any_of: Sequence[Identity] | None = None,
        acquire_timeout: float | None = None,
        priority: int = 0,
        wait_cooldown: bool = True,
    ) -> AsyncGenerator[ContextLease[B, C, Any]]:
        """Арендовать контекст identity (или любой из `any_of`) целиком — для site SDK, который сам управляет вкладками.

        Параметры ожидания те же, что у `page`.

        Эксклюзивно: выдаётся, когда у контекста нет других аренд; пока ждёт, новые вкладки этой
        identity не выдаются; пока держится, контекст не получает никто. Занимает один слот
        вкладки. Вкладки, которые откроет арендатор, он же и закрывает. Хуки `after_acquire` /
        `before_release` — только для аренд вкладок.
        """
        candidates = self._feasible(candidates_of(identity, any_of))
        self._check_candidates(candidates, wait_cooldown=wait_cooldown)
        requested = monotonic()
        grant = await self._acquire(
            candidates, limit=acquire_timeout, priority=priority, exclusive=True
        )
        self._warn_unpinned(grant.identity)
        try:
            await self._resources.acquire_context(grant)
            natives = self._natives(grant)
        except BaseException as error:
            self._outcomes.acquire_failed(grant, error)
            raise
        acquired = self._acquired(grant, requested)
        lease = ContextLease[B, C, Any](
            control=self._control,
            lease_id=grant.lease_id,
            identity=grant.identity,
            browser_id=grant.browser_id,
            generation=grant.generation,
            browser=natives.browser,
            context=natives.context,
            session=natives.session,
            proxy=natives.proxy,
        )
        self._active[grant.lease_id] = (grant, None)
        watch = self._observer.watch(grant)
        error: BaseException | None = None
        try:
            yield lease
        except BaseException as raised:
            error = raised
            revoked = watch.revoked(raised)
            if revoked is not None:
                raise revoked from raised
            raise
        finally:
            watch.stop()
            del self._active[grant.lease_id]
            await self._detached(
                self._outcomes.finish(grant, None, lease=lease, error=error, acquired=acquired)
            )

    # --- изменение на лету ------------------------------------------------------------

    async def resize(
        self, *, browsers: int | None = None, pages_per_browser: int | None = None
    ) -> None:
        """Изменить ёмкость на лету.

        Рост — сразу: новые браузеры запустятся по спросу, в каждом больше вкладок. Уменьшение
        браузеров — через дренаж: лишние (сначала самые пустые) перестают получать аренды,
        выданные дорабатывают, после этого браузер закрывается и слот убирается — `resize`
        ждёт этого. Меньше вкладок на браузер — новые аренды получат меньше, выданные не
        отбираются. Несовместимое значение (`browsers < min_browsers`) — `ConfigError`.
        """
        changes: dict[str, int] = {}
        if browsers is not None:
            changes["browsers"] = browsers
        if pages_per_browser is not None:
            changes["pages_per_browser"] = pages_per_browser
        async with self._resizing:
            topology = dataclasses.replace(self._requested.topology, **changes)
            self._apply(self._requested.replace(topology=topology))
            slots = [
                view
                for view in self._scheduler.browsers()
                if view.id not in self._supervisor.leaving
            ]
            target = self._config.topology.browsers
            for browser_id in new_slots(self._scheduler.browsers(), len(slots), target):
                self._scheduler.add_browser(browser_id)
                self._supervisor.add_browser(browser_id)
            victims = sorted(slots, key=removal_order)[: max(0, len(slots) - target)]
            self._settle()
            await asyncio.gather(*(self._supervisor.remove_browser(view.id) for view in victims))
            self._settle()

    def reconfigure(
        self,
        *,
        limits: Limits | None = None,
        lifecycle: Lifecycle | None = None,
        recycling: Recycling | None = None,
        recovery: Recovery | None = None,
    ) -> None:
        """Изменить лимиты, простой, смену и восстановление на лету: они действуют на следующие решения пула.

        Идущие операции доработают по старым: начатые запуски и входы не прерываются.
        Топология меняется `resize`, таймауты и остальные секции — только при создании пула.
        """
        self._apply(
            self._requested.replace(
                limits=limits, lifecycle=lifecycle, recycling=recycling, recovery=recovery
            )
        )

    def _apply(self, requested: PoolConfig) -> None:
        """Новый запрошенный конфиг: эффективный — по возможностям пары — во все компоненты."""
        config = effective_config(requested, self._capabilities)
        self._requested, self._config = requested, config
        self._scheduler.reconfigure(
            topology=config.topology,
            limits=config.limits,
            recycling=config.recycling,
            recovery=config.recovery,
        )
        self._resources.reconfigure(topology=config.topology, limits=config.limits)
        self._sessions.reconfigure(config.limits)
        self._proxy_broker.reconfigure(limits=config.limits, retries=config.recovery.proxy_retries)
        self._observer.reconfigure(config.limits)
        self._supervisor.reconfigure(config)
        self._settle()

    # --- высокоуровневый API ----------------------------------------------------------

    async def run[T](
        self,
        task: Callable[[PageLease[B, C, P, Any]], Awaitable[T]],
        identity: Identity | None = None,
        *,
        any_of: Sequence[Identity] | None = None,
        retries: int = 0,
        backoff: Backoff | None = None,
        retry_on: Collection[ErrorKind] = RETRIED_KINDS,
        acquire_timeout: float | None = None,
        priority: int = 0,
        wait_cooldown: bool = True,
    ) -> T:
        """Выполнить `task(lease)` на вкладке identity; упало по повторяемой причине — ещё раз.

        Каждая попытка — новая аренда: пул уже отреагировал на сбой (выбросил вкладку, вывел
        контекст, сменит прокси), и следующая получит исправное. Повтор — если вид сбоя из
        `retry_on` (по умолчанию `RETRIED_KINDS`) и попытки не кончились (`retries` — сколько
        повторов сверх первой), с паузой `backoff`. Иначе исключение последней попытки
        пробрасывается как есть. Ошибки самого пула (таймаут ожидания, остановка, блокировка)
        не повторяются; сбой прокси при открытии (`ProxyFailedError`) — повторяется.

        Повтор безопасен только для идемпотентных задач: вкладка могла упасть уже после
        отправленного письма или принятого платежа. Перед таким действием зовите `lease.commit()` —
        после него эта попытка не повторяется, исключение уходит наружу.
        """
        return await run_with_retries(
            lambda: self.page(
                identity,
                any_of=any_of,
                acquire_timeout=acquire_timeout,
                priority=priority,
                wait_cooldown=wait_cooldown,
            ),
            task,
            retries=retries,
            backoff=backoff,
            retry_on=retry_on,
            verdicts=self._verdicts,
            emit=self._bus.emit,
            first_key=first_key(identity, any_of),
        )

    @overload
    async def map[T](
        self,
        task: Callable[[PageLease[B, C, P, Any]], Awaitable[T]],
        identities: Iterable[Identity],
        *,
        concurrency: int = 8,
        retries: int = 0,
        backoff: Backoff | None = None,
        retry_on: Collection[ErrorKind] = RETRIED_KINDS,
        acquire_timeout: float | None = None,
        priority: int = 0,
        wait_cooldown: bool = True,
        return_exceptions: Literal[False] = False,
    ) -> list[T]: ...

    @overload
    async def map[T](
        self,
        task: Callable[[PageLease[B, C, P, Any]], Awaitable[T]],
        identities: Iterable[Identity],
        *,
        concurrency: int = 8,
        retries: int = 0,
        backoff: Backoff | None = None,
        retry_on: Collection[ErrorKind] = RETRIED_KINDS,
        acquire_timeout: float | None = None,
        priority: int = 0,
        wait_cooldown: bool = True,
        return_exceptions: Literal[True],
    ) -> list[T | Exception]: ...

    async def map[T](
        self,
        task: Callable[[PageLease[B, C, P, Any]], Awaitable[T]],
        identities: Iterable[Identity],
        *,
        concurrency: int = 8,
        retries: int = 0,
        backoff: Backoff | None = None,
        retry_on: Collection[ErrorKind] = RETRIED_KINDS,
        acquire_timeout: float | None = None,
        priority: int = 0,
        wait_cooldown: bool = True,
        return_exceptions: bool = False,
    ) -> list[T] | list[T | Exception]:
        """`run` для каждой identity, не больше `concurrency` разом; результаты — в порядке входа.

        Параметры `run` (повторы и ожидание выдачи) действуют на каждую задачу отдельно.

        Сбой одной задачи (после её повторов) отменяет остальные и пробрасывается — первый по
        времени. `return_exceptions=True` — не отменять: на месте упавшей задачи её исключение.
        """
        if concurrency < 1:
            msg = f"concurrency должен быть ≥ 1, получено {concurrency}"
            raise ValueError(msg)
        gate = asyncio.Semaphore(concurrency)

        async def one(identity: Identity) -> T | Exception:
            async with gate:
                try:
                    return await self.run(
                        task,
                        identity,
                        retries=retries,
                        backoff=backoff,
                        retry_on=retry_on,
                        acquire_timeout=acquire_timeout,
                        priority=priority,
                        wait_cooldown=wait_cooldown,
                    )
                except Exception as error:
                    if return_exceptions:
                        return error
                    raise

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(identity)) for identity in identities]
        except* Exception as failed:  # noqa: BLE001 — пробрасывается первое, как у gather
            raise first_failure(failed) from None
        return [task.result() for task in tasks]

    def _natives(self, grant: Grant) -> Natives[B, C]:
        browser, context = self._resources.natives(grant)
        return Natives(
            browser,
            context,
            session=self._resources.session(grant),
            proxy=self._resources.proxy(grant),
        )

    def _acquired(self, grant: Grant, requested: float) -> float:
        """Аренда состоялась: учёт открытия контекста и события. Возвращает момент выдачи."""
        if grant.new_context:
            self._scheduler.open_succeeded(grant.identity.key)
            self._observer.counters.contexts_opened += 1
            self._bus.emit(
                ContextOpened(
                    key=grant.identity.key, browser_id=grant.browser_id, generation=grant.generation
                )
            )
        acquired = monotonic()
        self._observer.acquired(acquired - requested)
        self._bus.emit(
            LeaseAcquired(
                lease_id=grant.lease_id,
                key=grant.identity.key,
                browser_id=grant.browser_id,
                generation=grant.generation,
                waited=acquired - requested,
            )
        )
        return acquired

    # --- то, что аренда просит у пула (через `_Control`) ---------------------------------

    def _attachments_of(self, lease_id: int) -> dict[str, object]:
        """Вложения вкладки живой аренды."""
        grant, page = self._active[lease_id]
        if page is None:
            msg = f"Аренда {lease_id} — контекст целиком, вложения бывают только у вкладок"
            raise PoolInvariantError(msg)
        return self._resources.attachments(grant, page)

    async def _retire_context(self, key: str, *, generation: int, reason: str) -> None:
        """Вывести контекст identity из работы; закроется, когда вернут все его вкладки."""
        if self._scheduler.retire(key, generation=generation):
            _logger.info(
                "Контекст %s (поколение %d) выводится из работы: %s", key, generation, reason
            )
            self._bus.emit(ContextRetired(key=key, generation=generation, reason=reason))
        self._settle()

    async def _cookies_of(self, lease_id: int, *, domain: str | None) -> tuple[Cookie, ...]:
        """Куки контекста живой аренды."""
        grant, _page = self._active[lease_id]
        return await self._resources.cookies(grant, domain=domain)

    # --- исход аренды ------------------------------------------------------------------

    async def _detached(self, tail: Coroutine[object, object, None]) -> None:
        """Довести хвост аренды до конца, что бы ни случилось с арендатором.

        Хвост идёт задачей пула: отмена арендатора (`pool.map` отменяет соседей, `asyncio.timeout`
        снаружи) обрывает только ожидание, а вкладка всё равно возвращается и слот освобождается.
        Остановка пула хвоста дожидается.
        """
        await asyncio.shield(self._spawn(tail))

    async def _conclude(
        self,
        grant: Grant,
        page: P,
        *,
        lease: PageLease[B, C, P, Any],
        error: BaseException | None,
        acquired: float,
    ) -> None:
        """Хвост аренды вкладки: хуки `before_release`, затем исход — сразу или после задержки."""
        try:
            await self._release_hooks(lease)  # аренда ещё жива: хуку доступны её куки и вложения
        finally:
            del self._active[grant.lease_id]
            if isinstance(error, Exception) and self.held_pages.holding:
                self._spawn(
                    self._finish_later(grant, page, lease=lease, error=error, acquired=acquired)
                )
            else:
                await self._outcomes.finish(
                    grant, page, lease=lease, error=error, acquired=acquired
                )

    async def _release_hooks(self, lease: PageLease[B, C, P, Any]) -> None:
        try:
            await self._hook_runner.run_before_release(lease)
        except Exception:
            # Состояние вкладки после упавшего хука неизвестно: в запас её не брать.
            _logger.warning(
                "Хук before_release упал; вкладка аренды %d выбрасывается",
                lease.lease_id,
                exc_info=True,
                extra={"lease_id": lease.lease_id, "browser_id": lease.browser_id},
            )
            lease.discard_page()

    async def _finish_later(
        self,
        grant: Grant,
        page: P,
        *,
        lease: PageLease[B, C, P, Any],
        error: Exception,
        acquired: float,
    ) -> None:
        """`hold_on_error`: вкладка остаётся открытой и занимает слот, потом аренда завершается."""
        _logger.warning(
            "Вкладка аренды %d (%s) задержана после %s — pool.held_pages.release() отпустит",
            grant.lease_id,
            grant.identity.key,
            type(error).__name__,
        )
        await self.held_pages.hold()
        await self._outcomes.finish(grant, page, lease=lease, error=error, acquired=acquired)

    def _warn_unpinned(self, identity: Identity) -> None:
        """`ProxyPolicy.sticky()` без источника прокси — аренда пойдёт напрямую: сказать об этом, один раз."""
        if self._direct_sticky_warned or self._has_proxy_source or identity.proxy.mode != "sticky":
            return
        if self._capabilities.proxy_scope == "external":
            return  # прокси задаёт вендор браузера
        self._direct_sticky_warned = True
        _logger.warning(
            "Identity %s просит закреплённый прокси (ProxyPolicy.sticky), а у пула нет proxy_source — "
            "она работает напрямую, с адреса хоста; так же пойдут остальные sticky-identity",
            identity.key,
        )

    def _feasible(self, candidates: tuple[Identity, ...]) -> tuple[Identity, ...]:
        """Кандидаты, которых драйвер способен обслужить. Никого — `UnsupportedRequirementError`.

        Невыполнимое не ждёт в очереди: драйвер не научится ему, сколько ни жди. Ошибка
        перечисляет все несоответствия всех кандидатов разом.
        """
        capabilities = self._capabilities
        problems = {identity.key: unmet(identity, capabilities) for identity in candidates}
        feasible = tuple(identity for identity in candidates if not problems[identity.key])
        if feasible:
            return feasible
        raise UnsupportedRequirementError(
            missing=tuple(problem for found in problems.values() for problem in found)
        )

    def _check_candidates(self, candidates: tuple[Identity, ...], *, wait_cooldown: bool) -> None:
        now = monotonic()
        statuses = [
            self._scheduler.identity_status(identity.key, now=now) for identity in candidates
        ]
        if all(status.blocked is not None for status in statuses):
            first = statuses[0]
            raise IdentityBlockedError(identity=first.key, reason=first.blocked or "")
        if wait_cooldown or any(
            status.blocked is None and status.cooling_until is None for status in statuses
        ):
            return
        cooling = [status for status in statuses if status.cooling_until is not None]
        soonest = min(cooling, key=lambda status: status.cooling_until or 0.0)
        raise IdentityCoolingDownError(
            identity=soonest.key, retry_after=(soonest.cooling_until or now) - now
        )

    def _fail_blocked_waiters(self) -> None:
        now = monotonic()
        for waiter, future in tuple(self._futures.items()):
            statuses = [
                self._scheduler.identity_status(identity.key, now=now)
                for identity in waiter.candidates
            ]
            if all(status.blocked is not None for status in statuses):
                self._scheduler.cancel(waiter)
                del self._futures[waiter]
                if not future.done():
                    future.set_exception(
                        IdentityBlockedError(
                            identity=statuses[0].key, reason=statuses[0].blocked or ""
                        )
                    )

    def _schedule_wake(self) -> None:
        """Разбудить очередь, когда кончится ближайшая пауза identity, которой ждут."""
        now = monotonic()
        at = self._scheduler.next_wakeup(now=now)
        if at is None:
            return
        if self._wake is not None and self._wake.when() <= at and not self._wake.cancelled():
            return
        if self._wake is not None:
            self._wake.cancel()
        self._wake = asyncio.get_running_loop().call_at(at, self._woke)

    def _woke(self) -> None:
        self._wake = None
        self._settle()

    # --- внутреннее --------------------------------------------------------------------

    async def _acquire(
        self,
        candidates: tuple[Identity, ...],
        *,
        limit: float | None,
        priority: int,
        exclusive: bool = False,
    ) -> Grant:
        if not self._accepting:
            raise PoolStoppedError
        if self._supervisor.unavailable():
            raise PoolUnavailableError
        waiter = self._scheduler.request(
            candidates, now=monotonic(), priority=priority, exclusive=exclusive
        )
        if waiter.grant is None:
            await self._wait(waiter, candidates, limit=limit)
        grant = waiter.grant
        if grant is None:  # pragma: no cover — future разрешается только выдачей или ошибкой
            msg = "Ожидание завершилось без аренды"
            raise PoolStoppedError(msg)
        if not self._accepting:
            # Выдали в последний момент, а пул уже останавливается: аренду вернуть.
            self._return(grant)
            raise PoolStoppedError
        self._drained.clear()
        return grant

    async def _wait(
        self, waiter: Waiter, candidates: tuple[Identity, ...], *, limit: float | None
    ) -> None:
        if limit is None:
            limit = self._config.timeouts.acquire
        future = asyncio.get_running_loop().create_future()
        self._futures[waiter] = future
        scope = asyncio.timeout(limit)
        try:
            async with scope:
                await self._await_watched(future, candidates)
        except TimeoutError as error:
            self._abandon(waiter)
            if scope.expired() and limit is not None:
                self._observer.counters.acquire_timeouts += 1
                raise AcquireTimeoutError(
                    timeout=limit, candidates=tuple(identity.key for identity in candidates)
                ) from error
            raise
        except BaseException:
            self._abandon(waiter)
            raise

    async def _await_watched(
        self, future: asyncio.Future[None], candidates: tuple[Identity, ...]
    ) -> None:
        """Ждать выдачи, раз в `acquire_watchdog` сообщая о долгом ожидании. Само ожидание не прерывается."""
        started = monotonic()
        while True:
            tick = asyncio.timeout(self._config.timeouts.acquire_watchdog)
            try:
                async with tick:
                    await asyncio.shield(future)
            except TimeoutError:
                if not tick.expired():
                    raise
                self._watchdog(candidates, monotonic() - started)
            else:
                return

    def _watchdog(self, candidates: tuple[Identity, ...], waited: float) -> None:
        keys = tuple(identity.key for identity in candidates)
        snapshot = self._observer.snapshot()
        _logger.warning(
            "Заявка на %s ждёт выдачи %.0f с; аренд %d из %d, в очереди %d",
            ", ".join(keys[:5]),
            waited,
            snapshot.leases_active,
            snapshot.capacity_healthy,
            snapshot.waiting,
        )
        self._bus.emit(
            AcquireWatchdog(
                waited=waited,
                candidates=keys,
                leases_active=snapshot.leases_active,
                waiting=snapshot.waiting,
            )
        )

    def _health_checked(self) -> None:
        self._bus.emit(PoolHealth(snapshot=self._observer.snapshot()))

    def _abandon(self, waiter: Waiter) -> None:
        self._futures.pop(waiter, None)
        self._scheduler.abandon(waiter, now=monotonic())
        self._settle()

    def _return(self, grant: Grant) -> None:
        self._scheduler.release(grant, now=monotonic())
        self._settle()

    def _settle(self) -> None:
        """После любого изменения: закрыть выведенное, раздать освободившееся ожидающим."""
        if self._scheduler.active_leases == 0:
            self._drained.set()
        if self._closed:
            return
        for waiter in self._scheduler.assign(now=monotonic()):
            future = self._futures.pop(waiter, None)
            if future is not None and not future.done():
                future.set_result(None)
            elif waiter.grant is not None:
                self._scheduler.release(waiter.grant, now=monotonic())
        # После выдачи: она сама вытесняет простаивающие контексты ради места.
        closures = self._scheduler.take_closures()
        if closures:
            self._spawn(self._close_contexts(closures))
        if self._scheduler.active_leases:
            self._drained.clear()
        self._schedule_wake()
        self._supervisor.notify()

    def _browser_launched(self, browser_id: str, browser: B) -> None:
        # Супервизор создаётся после физического слоя, поэтому связь — через метод пула.
        self._supervisor.browser_launched(browser_id, browser)

    def _browser_closing(self, browser_id: str) -> None:
        self._supervisor.browser_closing(browser_id)

    def _fail_waiters(self) -> None:
        """Пул недоступен: всем ожидающим — отказ, ждать нечего."""
        for waiter, future in tuple(self._futures.items()):
            self._scheduler.cancel(waiter)
            if not future.done():
                future.set_exception(PoolUnavailableError())
        self._futures.clear()

    async def _close_contexts(self, closures: Sequence[ContextView]) -> None:
        for context in closures:
            await self._resources.close_context(context.key, generation=context.generation)
            self._scheduler.forget(context.key, generation=context.generation)
            if not self._scheduler.has_context(context.key):
                self._resources.forget_identity(context.key)
            self._observer.counters.contexts_closed += 1
            self._bus.emit(ContextClosed(key=context.key, generation=context.generation))
        self._settle()

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _shutdown(self, *, wait: float) -> None:
        if self._closed:
            return
        self._accepting = False
        self.held_pages.close()
        if self._wake is not None:
            self._wake.cancel()
            self._wake = None
        await self._supervisor.stop()
        for waiter, future in tuple(self._futures.items()):
            self._scheduler.cancel(waiter)
            if not future.done():
                future.set_exception(PoolStoppedError())
        self._futures.clear()
        if wait > 0 and self._scheduler.active_leases:
            try:
                async with asyncio.timeout(wait):
                    await self._drained.wait()
            except TimeoutError:
                _logger.warning(
                    "Пул остановлен, не дождавшись %d аренд за %g с",
                    self._scheduler.active_leases,
                    wait,
                )
        self._closed = True
        await self._drain_tasks()
        await self._resources.aclose()
        await self._drain_tasks()
        await self._shutdown_driver()
        self._guard.close()

    async def _shutdown_driver(self) -> None:
        if not self._prepared:
            return
        self._prepared = False
        try:
            async with asyncio.timeout(self._config.timeouts.close):
                await self._driver.shutdown()
        except Exception:
            _logger.warning("Драйвер не освободил ресурсы после остановки пула", exc_info=True)

    async def _drain_tasks(self) -> None:
        # Обработчики событий порождают задачи и во время остановки: ждать, пока новые не кончатся.
        while self._tasks:
            waited = tuple(self._tasks)
            await asyncio.gather(*waited, return_exceptions=True)
            # Сами, не дожидаясь колбэка задачи: ожидание уже завершённой задачи цикл не отпускает,
            # и колбэк, который убрал бы её из набора, не получил бы хода.
            self._tasks.difference_update(waited)


__all__ = ["BrowserPool"]
