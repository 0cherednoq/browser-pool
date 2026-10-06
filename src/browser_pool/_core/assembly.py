"""Сборка ядра пула: компоненты и их связи в одном месте (composition root).

`BrowserPool` — фасад операций; кто с кем связан (планировщик, физические ресурсы, супервизор, сессии,
прокси, наблюдение) решает эта сборка. Компоненты общаются с фасадом только через переданные
обратные вызовы (`CoreCallbacks`): раздать освободившееся, отказать ожидающим, породить задачу.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from browser_pool._core.contexts import PhysicalResources
from browser_pool._core.facade_support import context_spec, default_probe
from browser_pool._core.observer import Observer
from browser_pool._core.proxies import ProxyBroker
from browser_pool._core.resources import ResourceGuard
from browser_pool._core.scheduler import Scheduler
from browser_pool._core.sessions import Sessions
from browser_pool._core.supervisor import Supervisor
from browser_pool.driver import LaunchSpec
from browser_pool.events import EventBus
from browser_pool.locks import LocalIdentityLock
from browser_pool.procguard import ProcessGuard
from browser_pool.state import MemoryStateStore

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from browser_pool._core.capabilities import Owner
    from browser_pool.config import PoolConfig
    from browser_pool.driver import Driver, DriverCapabilities
    from browser_pool.flow import SessionFlow
    from browser_pool.hooks import HookRunner
    from browser_pool.host import HostProbe
    from browser_pool.locks import IdentityLock
    from browser_pool.provider import EndpointProvider
    from browser_pool.proxies import ProxyChecker, ProxySource
    from browser_pool.state import StateStore


@dataclass(frozen=True, slots=True)
class CoreCallbacks:
    """Что ядру нужно от фасада."""

    spawn: Callable[[Coroutine[object, object, None]], object]
    """Породить фоновую задачу, принадлежащую пулу."""
    settle: Callable[[], None]
    """После изменения: закрыть выведенное, раздать освободившееся."""
    fail_waiters: Callable[[], None]
    """Пул недоступен: отказать всем ожидающим."""
    health_checked: Callable[[], None]
    """Проверка здоровья прошла."""
    is_proxy_fault: Callable[[BaseException], bool]
    """Ошибка — сбой прокси."""
    config: Callable[[], PoolConfig]
    """Действующий (эффективный) конфиг — читается в момент решения."""
    windows_shown: Callable[[], bool]
    """Браузеры запускаются с окнами: секция окон включена и показать окна есть где."""


@dataclass(frozen=True, slots=True)
class Core:
    """Собранные компоненты ядра."""

    bus: EventBus
    store: StateStore
    guard: ProcessGuard
    sessions: Sessions[Any, Any]
    scheduler: Scheduler
    proxy_broker: ProxyBroker
    resources: PhysicalResources[Any, Any, Any]
    resource_guard: ResourceGuard
    supervisor: Supervisor
    observer: Observer


def build_core[B, C, P](
    *,
    driver: Driver[B, C, P],
    config: PoolConfig,
    capabilities: DriverCapabilities,
    owner: Owner | None,
    hooks: HookRunner[B, C, P],
    callbacks: CoreCallbacks,
    flow: SessionFlow[C, P, Any] | None,
    state_store: StateStore | None,
    proxy_source: ProxySource | None,
    process_guard: ProcessGuard | None,
    identity_lock: IdentityLock | None,
    provider: EndpointProvider | None,
    host_probe: HostProbe | None,
    proxy_checker: ProxyChecker | None,
) -> Core:
    """Собрать планировщик, ресурсы, сессии, прокси, супервизор и наблюдение."""
    bus = EventBus(callbacks.spawn)
    store: StateStore = state_store if state_store is not None else MemoryStateStore()
    guard = process_guard if process_guard is not None else ProcessGuard()
    sessions = Sessions(
        driver,
        state_support=capabilities.state_support,
        store=store,
        default_flow=flow,
        timeouts=config.timeouts,
        limits=config.limits,
        emit=bus.emit,
    )
    scheduler = Scheduler(
        config.topology,
        limits=config.limits,
        recycling=config.recycling,
        recovery=config.recovery,
        owner=owner,
    )
    proxy_broker = ProxyBroker(
        proxy_source,
        capabilities=capabilities,
        limits=config.limits,
        retries=config.recovery.proxy_retries,
        emit=bus.emit,
    )
    supervisors: list[Supervisor] = []  # физический слой создаётся раньше супервизора, а зовёт его
    current = callbacks.config
    resources: PhysicalResources[B, C, P] = PhysicalResources(
        driver,
        topology=config.topology,
        timeouts=config.timeouts,
        launch_spec=lambda _browser_id: LaunchSpec(
            headless=not callbacks.windows_shown(),
            slow_mo=current().debug.slow_mo,
            keep_background_active=current().debug.keep_background_active,
        ),
        context_spec=lambda identity: context_spec(
            identity,
            fit_window=callbacks.windows_shown() and current().windows.fit_viewport,
            window_per_page=callbacks.windows_shown()
            and current().windows.mode == "per_page"
            and capabilities.new_window,
        ),
        limits=config.limits,
        on_launch=lambda browser_id, browser: supervisors[0].browser_launched(browser_id, browser),  # noqa: PLW0108
        hooks=hooks,
        sessions=sessions,
        proxies=proxy_broker,
        is_proxy_fault=callbacks.is_proxy_fault,
        guard=guard,
        identity_lock=identity_lock if identity_lock is not None else LocalIdentityLock(),
        owner=owner,
        on_close=lambda browser_id: supervisors[0].browser_closing(browser_id),  # noqa: PLW0108
        provider=provider,
        capabilities=capabilities,
        geo=proxy_checker,
    )
    resource_guard = ResourceGuard(
        config.resources,
        host_probe if host_probe is not None or not config.resources.active else default_probe(),
    )
    supervisor = Supervisor(
        driver,
        guard=resource_guard,
        scheduler=scheduler,
        resources=resources,
        config=config,
        settle=callbacks.settle,
        on_unavailable=callbacks.fail_waiters,
        emit=bus.emit,
        after_check=callbacks.health_checked,
    )
    supervisors.append(supervisor)
    observer = Observer(
        scheduler=scheduler,
        resources=resources,
        limits=config.limits,
        emit=bus.emit,
        restarts=lambda: supervisor.restarts,
        pressure=lambda: supervisor.pressure,
    )
    return Core(
        bus=bus,
        store=store,
        guard=guard,
        sessions=sessions,
        scheduler=scheduler,
        proxy_broker=proxy_broker,
        resources=resources,
        resource_guard=resource_guard,
        supervisor=supervisor,
        observer=observer,
    )


__all__ = ["Core", "CoreCallbacks", "build_core"]
