"""Супервизор: здоровье браузеров, восстановление, плановый перезапуск, простой.

Машина состояний браузера (`BrowserState`) — единственный путь смены его состояния:

    healthy ──► draining ──► restarting ──► healthy
       │           │             │
       └──► quarantined ◄────────┘ (попытка не удалась)
                   └──► restarting

- **Падение** — событие `disconnected` от драйвера или провал `ping` под таймаутом — отправляет
  браузер в карантин: новых аренд нет, его контексты выводятся из работы, занятые аренды
  дорабатывают. Своё же закрытие (простой, перезапуск) карантином не считается.
- **Восстановление**: дождаться, пока у браузера не останется ни аренд, ни контекстов, закрыть
  (не закрылся — добить), запустить заново; не вышло — пауза, растущая вдвое до потолка.
  Попытки кончились — браузер остаётся в карантине, а следующую серию попыток начинает проверка
  здоровья через `restart_backoff.maximum`: временный сбой запуска не выводит слот навсегда.
  Если в карантине все и никто не восстанавливается, пул недоступен: ожидающим и новым заявкам —
  `PoolUnavailableError`, пока очередная серия не начнётся.
- **Плановый перезапуск** по числу аренд или времени жизни процесса: порог с отрицательным
  джиттером (только раньше, никогда позже), по одному браузеру и только когда остальные здоровы.
- **Проверка здоровья** заодно закрывает простаивающие вкладки, контексты и браузеры и держит
  запущенными `min_browsers`.
- **Давление на хост** (`Resources`): под давлением памяти или CPU рост пула закрыт, `min_browsers`
  не добирается, при `pressure_action="shrink"` закрывается простаивающее — все тёплые вкладки,
  самый холодный контекст и браузеры без контекстов; распухший браузер перезапускается с дренажом.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from browser_pool.clock import monotonic
from browser_pool.errors import PoolInvariantError
from browser_pool.events import (
    BrowserIdleClosed,
    BrowserQuarantined,
    BrowserRecycled,
    BrowserRestarted,
    BrowserStarted,
    PoolUnavailable,
    ResourcePressure,
)
from browser_pool.snapshot import BrowserState

if TYPE_CHECKING:
    from collections.abc import Callable

    from browser_pool._core.contexts import PhysicalResources
    from browser_pool._core.resources import ResourceGuard
    from browser_pool._core.scheduler import BrowserView, Scheduler
    from browser_pool.config import PoolConfig
    from browser_pool.driver import Driver
    from browser_pool.events import PoolEvent

_logger = logging.getLogger(__name__)

_TRANSITIONS: dict[BrowserState, frozenset[BrowserState]] = {
    BrowserState.healthy: frozenset(
        {BrowserState.draining, BrowserState.quarantined, BrowserState.stopped}
    ),
    BrowserState.draining: frozenset(
        {
            BrowserState.healthy,
            BrowserState.restarting,
            BrowserState.quarantined,
            BrowserState.stopped,
        }
    ),
    BrowserState.quarantined: frozenset({BrowserState.restarting, BrowserState.stopped}),
    BrowserState.restarting: frozenset(
        {BrowserState.healthy, BrowserState.quarantined, BrowserState.stopped}
    ),
    BrowserState.starting: frozenset(
        {BrowserState.healthy, BrowserState.quarantined, BrowserState.stopped}
    ),
    BrowserState.stopped: frozenset(),
}


def check_transition(current: BrowserState, target: BrowserState) -> None:
    """Допустим ли переход. Недопустимый — ошибка библиотеки, а не ситуация."""
    if target not in _TRANSITIONS[current]:
        msg = f"Недопустимый переход состояния браузера: {current} → {target}"
        raise PoolInvariantError(msg)


@dataclass(eq=False, slots=True)
class _Tracked:
    """То, что супервизор знает о процессе браузера сверх планировщика."""

    process: object | None = None
    started_at: float = 0.0
    leases_at_start: int = 0
    recycle_after_leases: float | None = None
    recycle_after_uptime: float | None = None
    idle_since: float | None = None
    rebuild: asyncio.Task[None] | None = None
    retry_at: float | None = None
    """Когда начать следующую серию попыток восстановления; `None` — серия не ждёт."""


@dataclass(eq=False, slots=True)
class _Waits:
    """Ожидания условий, которые проверяются после каждого изменения в пуле."""

    pending: list[tuple[Callable[[], bool], asyncio.Future[None]]] = field(
        default_factory=list["tuple[Callable[[], bool], asyncio.Future[None]]"]
    )

    async def until(self, predicate: Callable[[], bool]) -> None:
        if predicate():
            return
        future = asyncio.get_running_loop().create_future()
        entry = (predicate, future)
        self.pending.append(entry)
        try:
            await future
        finally:
            if entry in self.pending:
                self.pending.remove(entry)

    def check(self) -> None:
        for predicate, future in tuple(self.pending):
            if not future.done() and predicate():
                future.set_result(None)


class Supervisor:
    """Следит за браузерами пула и восстанавливает их."""

    def __init__(
        self,
        driver: Driver[Any, Any, Any],
        *,
        scheduler: Scheduler,
        resources: PhysicalResources[Any, Any, Any],
        config: PoolConfig,
        settle: Callable[[], None],
        on_unavailable: Callable[[], None],
        emit: Callable[[PoolEvent], None],
        after_check: Callable[[], None],
        rng: random.Random | None = None,
        guard: ResourceGuard | None = None,
    ) -> None:
        self._driver = driver
        self._scheduler = scheduler
        self._resources = resources
        self._config = config
        self._settle = settle
        self._on_unavailable = on_unavailable
        self._emit = emit
        self._after_check = after_check
        self._guard = guard
        self._rng = rng or random.Random()  # noqa: S311 — джиттер, не криптография
        self._tracked = {view.id: _Tracked() for view in scheduler.browsers()}
        self._leaving: dict[str, asyncio.Task[None]] = {}
        """Слоты, которые убираются (`resize`): дренаж и закрытие."""
        self._waits = _Waits()
        self._loop_task: asyncio.Task[None] | None = None
        self._stopped = False
        self.restarts: int = 0
        """Сколько раз браузер восстановлен или перезапущен по плану."""

    # --- жизненный цикл ----------------------------------------------------------------

    async def start(self) -> None:
        """Поднять `min_browsers` и запустить проверку здоровья."""
        await self._ensure_min_browsers()
        self._loop_task = asyncio.create_task(self._health_loop(), name="browser-pool-health")

    async def stop(self) -> None:
        """Остановить проверку здоровья и восстановления. После этого события браузеров игнорируются."""
        self._stopped = True
        tasks = [
            task
            for task in (self._loop_task, *self._rebuilds(), *self._leaving.values())
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None

    def unavailable(self) -> bool:
        """Все браузеры в карантине и никто не восстанавливается."""
        return (
            all(view.state is BrowserState.quarantined for view in self._scheduler.browsers())
            and not self._rebuilds()
        )

    def reconfigure(self, config: PoolConfig) -> None:
        """Новый конфиг — для следующих решений: пороги перезапуска, простоя, проверок."""
        self._config = config

    @property
    def leaving(self) -> frozenset[str]:
        """Слоты, которые сейчас убираются."""
        return frozenset(self._leaving)

    def add_browser(self, browser_id: str) -> None:
        """Новый слот — под наблюдение (браузер в нём запустится лениво)."""
        self._tracked[browser_id] = _Tracked()

    async def remove_browser(self, browser_id: str) -> None:
        """Убрать слот: без новых аренд, дождаться выданных, закрыть браузер, забыть слот."""
        task = self._leaving.get(browser_id)
        if task is None:
            task = asyncio.create_task(
                self._remove(browser_id), name=f"browser-pool-remove-{browser_id}"
            )
            self._leaving[browser_id] = task
        await asyncio.shield(task)

    async def _remove(self, browser_id: str) -> None:
        tracked = self._tracked[browser_id]
        if tracked.rebuild is not None and not tracked.rebuild.done():
            tracked.rebuild.cancel()
            await asyncio.gather(tracked.rebuild, return_exceptions=True)
        if self._scheduler.browser(browser_id).state is BrowserState.healthy:
            self._set_state(browser_id, BrowserState.draining)
        self._scheduler.retire_browser(browser_id)
        self._settle()
        try:
            await self._waits.until(lambda: self._drained(browser_id))
            await self._close(browser_id)
            self._set_state(browser_id, BrowserState.stopped)
            self._scheduler.remove_browser(browser_id)
            self._resources.forget_browser(browser_id)
            del self._tracked[browser_id]
            _logger.info("Слот %s убран", browser_id, extra={"browser_id": browser_id})
        finally:
            self._leaving.pop(browser_id, None)
        self._settle()

    def notify(self) -> None:
        """В пуле что-то изменилось: проверить, не дождался ли кто-нибудь своего условия."""
        self._waits.check()

    # --- события -----------------------------------------------------------------------

    def browser_launched(self, browser_id: str, process: object) -> None:
        """Физический браузер запущен: запомнить момент и пороги, подписаться на падение."""
        tracked = self._tracked[browser_id]
        recycling = self._config.recycling
        tracked.process = process
        tracked.started_at = monotonic()
        view = self._scheduler.browser(browser_id)
        # Аренды, ради которых браузер запускают, выданы раньше запуска — но работать будут в нём.
        tracked.leases_at_start = view.leases_total - view.active
        tracked.idle_since = None
        by_leases = (
            recycling.persistent_browser_max_leases
            if self._resources.persistent(browser_id)
            else recycling.browser_max_leases
        )
        tracked.recycle_after_leases = self._jittered(by_leases)
        tracked.recycle_after_uptime = self._jittered(recycling.browser_max_age)
        self._driver.on_disconnect(process, lambda: self._disconnected(browser_id, process))
        self._emit(BrowserStarted(browser_id=browser_id))

    def browser_closing(self, browser_id: str) -> None:
        """Пул сам закрывает браузер под перезапуск: его отключение — не падение."""
        self._tracked[browser_id].process = None

    def quarantine(self, browser_id: str, reason: str) -> None:
        """Браузер умер или завис: вывести из работы целиком и восстанавливать."""
        if self._stopped or browser_id in self._leaving:
            return  # убираемый слот не восстанавливают: он и так закрывается
        state = self._scheduler.browser(browser_id).state
        if state not in {BrowserState.healthy, BrowserState.draining}:
            return
        _logger.warning(
            "Браузер %s в карантине: %s", browser_id, reason, extra={"browser_id": browser_id}
        )
        self._set_state(browser_id, BrowserState.quarantined)
        self._emit(BrowserQuarantined(browser_id=browser_id, reason=reason))
        self._scheduler.retire_browser(browser_id)
        self._start_rebuild(browser_id)
        self._settle()

    # --- проверка здоровья -------------------------------------------------------------

    async def check(self) -> None:
        """Один проход проверки здоровья."""
        await self._ping_all()
        self._retry_quarantined()
        await self._check_resources()
        lifecycle = self._config.lifecycle
        if lifecycle.page_idle_ttl is not None:
            await self._resources.expire_idle_pages(idle_ttl=lifecycle.page_idle_ttl)
        if lifecycle.context_idle_ttl is not None:
            self._scheduler.retire_idle(now=monotonic(), idle_ttl=lifecycle.context_idle_ttl)
        if lifecycle.state_save_interval is not None:
            await self._resources.save_due_states(interval=lifecycle.state_save_interval)
        self._settle()
        self._recycle_one_if_due()
        await self._close_idle_browsers()
        await self._ensure_min_browsers()
        self._after_check()

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.lifecycle.healthcheck_interval)
            try:
                await self.check()
            except Exception:
                _logger.exception("Проверка здоровья пула упала; следующая — по расписанию")

    async def _ping_all(self) -> None:
        for view in self._scheduler.browsers():
            process = self._tracked[view.id].process
            if view.state is not BrowserState.healthy or process is None:
                continue
            try:
                async with asyncio.timeout(self._config.timeouts.ping):
                    alive = await self._driver.ping(process)
            except Exception:  # noqa: BLE001 — любой отказ ping значит одно: браузер не отвечает
                alive = False
            if not alive and self._tracked[view.id].process is process:
                self.quarantine(view.id, "не отвечает на ping")

    # --- восстановление и перезапуск ---------------------------------------------------

    def _recycle_one_if_due(self) -> None:
        views = self._scheduler.browsers()
        if any(view.state is not BrowserState.healthy for view in views) or self._rebuilds():
            return
        now = monotonic()
        for view in views:
            tracked = self._tracked[view.id]
            if tracked.process is None:
                continue
            by_leases = (
                tracked.recycle_after_leases is not None
                and view.leases_total - tracked.leases_at_start >= tracked.recycle_after_leases
            )
            by_uptime = (
                tracked.recycle_after_uptime is not None
                and now - tracked.started_at >= tracked.recycle_after_uptime
            )
            if by_leases or by_uptime:
                self._recycle(view.id, "leases" if by_leases else "uptime")
                return

    def _recycle(self, browser_id: str, reason: str) -> None:
        """Перезапуск с дренажом: новых аренд нет, выданные дорабатывают, потом — заново."""
        _logger.info(
            "Перезапуск браузера %s (%s)", browser_id, reason, extra={"browser_id": browser_id}
        )
        self._emit(BrowserRecycled(browser_id=browser_id, reason=reason))
        self._set_state(browser_id, BrowserState.draining)
        self._scheduler.retire_browser(browser_id)
        self._start_rebuild(browser_id)
        self._settle()

    # --- давление на хост --------------------------------------------------------------

    @property
    def pressure(self) -> str | None:
        """Что давит на хост; `None` — давления нет или замеров нет."""
        return self._guard.pressure if self._guard is not None else None

    async def _check_resources(self) -> None:
        guard = self._guard
        if guard is None or not guard.active:
            return
        pids = {
            view.id: pid
            for view in self._scheduler.browsers()
            if (process := self._tracked[view.id].process) is not None
            and (pid := self._driver.pid(process)) is not None
        }
        report = await guard.sample(pids)
        if report.changed:
            self._scheduler.allow_growth(report.pressure is None)
            if report.pressure is not None:
                _logger.warning("Хост под давлением, пул не растёт: %s", report.pressure)
            else:
                _logger.info("Давление на хост снято, пул снова растёт")
            self._emit(
                ResourcePressure(
                    reason=report.pressure,
                    free_memory_mb=report.free_memory_mb,
                    cpu_percent=report.cpu_percent,
                )
            )
            self._settle()
        if report.pressure is not None and self._config.resources.pressure_action == "shrink":
            await self._shrink()
        for browser_id in report.heavy:
            if self._scheduler.browser(browser_id).state is BrowserState.healthy:
                self._recycle(browser_id, "rss")

    async def _shrink(self) -> None:
        """Под давлением: закрыть тёплые вкладки, самый холодный контекст, браузеры без контекстов."""
        await self._resources.expire_idle_pages(idle_ttl=0.0)
        if self._scheduler.retire_coldest():
            self._settle()
        await self._close_idle_browsers(ttl=0.0)

    def _start_rebuild(self, browser_id: str) -> None:
        tracked = self._tracked[browser_id]
        if tracked.rebuild is None or tracked.rebuild.done():
            tracked.rebuild = asyncio.create_task(
                self._rebuild(browser_id), name=f"browser-pool-rebuild-{browser_id}"
            )

    async def _rebuild(self, browser_id: str) -> None:
        await self._waits.until(lambda: self._drained(browser_id))
        await self._close(browser_id)
        recovery = self._config.recovery
        for attempt in range(1, recovery.restart_max_attempts + 1):
            self._set_state(browser_id, BrowserState.restarting)
            try:
                async with asyncio.timeout(self._config.timeouts.restart):
                    await self._resources.ensure_browser(browser_id)
            except Exception:
                _logger.warning(
                    "Браузер %s не поднялся (попытка %d)", browser_id, attempt, exc_info=True
                )
                self._set_state(browser_id, BrowserState.quarantined)
                if attempt < recovery.restart_max_attempts:
                    await asyncio.sleep(recovery.restart_backoff.delay(attempt - 1))
                continue
            self._set_state(browser_id, BrowserState.healthy)
            self.restarts += 1
            _logger.info("Браузер %s снова в строю", browser_id, extra={"browser_id": browser_id})
            self._emit(BrowserRestarted(browser_id=browser_id))
            self._settle()
            return
        if self._scheduler.browser(browser_id).state is not BrowserState.quarantined:
            self._set_state(browser_id, BrowserState.quarantined)
        tracked = self._tracked[browser_id]
        tracked.rebuild = None
        if recovery.restart_max_attempts:
            pause = recovery.restart_backoff.maximum
            tracked.retry_at = monotonic() + pause
            _logger.error(
                "Браузер %s оставлен в карантине: попытки кончились, следующая серия — через %g с",
                browser_id,
                pause,
            )
        else:
            _logger.error("Браузер %s оставлен в карантине: восстановление выключено", browser_id)
        if self.unavailable():
            self._emit(PoolUnavailable())
            self._on_unavailable()

    def _retry_quarantined(self) -> None:
        """Новая серия попыток для слотов, у которых прошлая кончилась ничем."""
        now = monotonic()
        for view in self._scheduler.browsers():
            tracked = self._tracked[view.id]
            if tracked.retry_at is None or now < tracked.retry_at or view.id in self._leaving:
                continue
            tracked.retry_at = None
            if view.state is BrowserState.quarantined:
                _logger.info("Браузер %s: новая серия попыток восстановления", view.id)
                self._start_rebuild(view.id)

    async def _close(self, browser_id: str) -> None:
        # Сначала забыть процесс: своё же закрытие не должно выглядеть падением.
        self._tracked[browser_id].process = None
        await self._resources.close_browser(browser_id)

    def _drained(self, browser_id: str) -> bool:
        view = self._scheduler.browser(browser_id)
        return view.active == 0 and view.contexts == 0

    # --- простой и минимум -------------------------------------------------------------

    async def _close_idle_browsers(self, ttl: float | None = None) -> None:
        """Закрыть браузеры без контекстов дольше `ttl` (по умолчанию — `browser_idle_ttl`)."""
        if ttl is None:
            ttl = self._config.lifecycle.browser_idle_ttl
        if ttl is None:
            return
        now = monotonic()
        for view in self._scheduler.browsers():
            if not self._idle_for(view, ttl=ttl, now=now):
                continue
            if self._launched() <= self._config.topology.min_browsers:
                return
            self._set_state(view.id, BrowserState.draining)
            await self._close(view.id)
            self._set_state(view.id, BrowserState.healthy)
            _logger.info("Простаивающий браузер %s закрыт", view.id, extra={"browser_id": view.id})
            self._emit(BrowserIdleClosed(browser_id=view.id))

    def _idle_for(self, view: BrowserView, *, ttl: float, now: float) -> bool:
        """Запущенный здоровый браузер без контекстов и аренд простаивает не меньше `ttl`."""
        tracked = self._tracked[view.id]
        if view.state is not BrowserState.healthy or tracked.process is None:
            return False
        if view.contexts or view.active:
            tracked.idle_since = None
            return False
        if tracked.idle_since is None:
            tracked.idle_since = now
        return now - tracked.idle_since >= ttl

    async def _ensure_min_browsers(self) -> None:
        if self.pressure is not None:
            return  # под давлением пул не растёт, и минимум тоже ждёт
        wanted = self._config.topology.min_browsers - self._launched()
        for view in self._scheduler.browsers():
            if wanted <= 0:
                return
            if view.state is not BrowserState.healthy or self._tracked[view.id].process is not None:
                continue
            try:
                await self._resources.ensure_browser(view.id)
            except Exception:  # noqa: BLE001 — не поднялся: пусть его восстановит карантин
                self.quarantine(view.id, "не запустился при подъёме минимума")
            wanted -= 1

    # --- служебное ---------------------------------------------------------------------

    def _disconnected(self, browser_id: str, process: object) -> None:
        tracked = self._tracked.get(browser_id)  # слот мог уже быть убран (`resize`)
        if tracked is not None and tracked.process is process:
            self.quarantine(browser_id, "соединение с браузером потеряно")

    def _set_state(self, browser_id: str, state: BrowserState) -> None:
        check_transition(self._scheduler.browser(browser_id).state, state)
        self._scheduler.set_browser_state(browser_id, state)

    def _launched(self) -> int:
        return sum(tracked.process is not None for tracked in self._tracked.values())

    def _rebuilds(self) -> list[asyncio.Task[None]]:
        return [
            tracked.rebuild
            for tracked in self._tracked.values()
            if tracked.rebuild is not None and not tracked.rebuild.done()
        ]

    def _jittered(self, threshold: float | None) -> float | None:
        if threshold is None:
            return None
        return threshold * (1 - self._config.recycling.recycle_jitter * self._rng.random())


__all__ = ["Supervisor", "check_transition"]
