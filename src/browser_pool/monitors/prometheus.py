"""Метрики пула в Prometheus (extra `[prometheus]`): только из событий пула.

    metrics = PrometheusMetrics()
    detach = metrics.attach(pool, name="mail")   # подписка на шину событий пула
    prometheus_client.start_http_server(9100)

Счётчики и гистограммы копятся из событий аренд, контекстов, браузеров, прокси и повторов;
ёмкость и очередь (гауджи) обновляются по `PoolHealth` — после каждой проверки здоровья. Метки —
только пул (`pool` — имя из `attach`), виды сбоев, события и состояния браузеров: ключи identity в метки
не идут (кардинальность). Несколько пулов — один `PrometheusMetrics` и `attach(pool, name=…)` на каждый.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from browser_pool.events import (
    BrowserIdleClosed,
    BrowserQuarantined,
    BrowserRecycled,
    BrowserRestarted,
    BrowserStarted,
    ContextOpened,
    ContextRetired,
    LeaseAcquired,
    LeaseReleased,
    OpenFailed,
    PoolEvent,
    PoolHealth,
    ProxyFailed,
    ResourcePressure,
    TaskRetried,
)
from browser_pool.snapshot import BrowserState

if TYPE_CHECKING:
    from collections.abc import Callable

    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

    from browser_pool.pool import BrowserPool

_BROWSER_EVENTS: dict[type[PoolEvent], str] = {
    BrowserStarted: "started",
    BrowserQuarantined: "quarantined",
    BrowserRestarted: "restarted",
    BrowserRecycled: "recycled",
    BrowserIdleClosed: "idle_closed",
}
_WAIT_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)
_HELD_BUCKETS = (0.1, 0.5, 1, 5, 10, 30, 60, 120, 300, 600, 1800)


class PrometheusMetrics:
    """Метрики пулов по их событиям; у каждой метрики есть метка `pool` — имя пула из `attach`."""

    def __init__(
        self, *, registry: CollectorRegistry | None = None, namespace: str = "browser_pool"
    ) -> None:
        """`registry` — свой реестр (по умолчанию общий `prometheus_client.REGISTRY`)."""
        import prometheus_client as prom

        target = registry if registry is not None else prom.REGISTRY
        options: dict[str, Any] = {"namespace": namespace, "registry": target}
        self._attached: set[str] = set()
        self.leases: Counter = prom.Counter(
            "leases", "Завершённые аренды по исходу", ["pool", "outcome"], **options
        )
        self.lease_wait: Histogram = prom.Histogram(
            "lease_wait_seconds",
            "Ожидание выдачи аренды",
            ["pool"],
            buckets=_WAIT_BUCKETS,
            **options,
        )
        self.lease_held: Histogram = prom.Histogram(
            "lease_held_seconds",
            "Сколько держали аренду",
            ["pool"],
            buckets=_HELD_BUCKETS,
            **options,
        )
        self.contexts_opened: Counter = prom.Counter(
            "contexts_opened", "Открытые контексты", ["pool"], **options
        )
        self.contexts_retired: Counter = prom.Counter(
            "contexts_retired", "Выведенные из работы", ["pool"], **options
        )
        self.open_failures: Counter = prom.Counter(
            "open_failures", "Контексты, которые не открылись", ["pool"], **options
        )
        self.browser_events: Counter = prom.Counter(
            "browser_events", "События браузеров", ["pool", "event"], **options
        )
        self.proxy_failures: Counter = prom.Counter(
            "proxy_failures", "Сбои прокси", ["pool"], **options
        )
        self.task_retries: Counter = prom.Counter(
            "task_retries", "Повторы pool.run по виду сбоя", ["pool", "kind"], **options
        )
        self.leases_active: Gauge = prom.Gauge(
            "leases_active", "Аренды сейчас", ["pool"], **options
        )
        self.waiting: Gauge = prom.Gauge("waiting", "Заявки в очереди", ["pool"], **options)
        self.capacity: Gauge = prom.Gauge(
            "capacity", "Ёмкость в вкладках", ["pool", "scope"], **options
        )
        self.browsers: Gauge = prom.Gauge(
            "browsers", "Браузеры по состоянию", ["pool", "state"], **options
        )
        self.under_pressure: Gauge = prom.Gauge(
            "under_pressure", "Хост под давлением: 1 — да", ["pool"], **options
        )
        self._counted: dict[type[PoolEvent], Counter] = {
            ContextOpened: self.contexts_opened,
            ContextRetired: self.contexts_retired,
            OpenFailed: self.open_failures,
            ProxyFailed: self.proxy_failures,
        }

    def attach(
        self, pool: BrowserPool[Any, Any, Any], *, name: str = "default"
    ) -> Callable[[], None]:
        """Подписаться на события пула под именем `name`. Возвращает отписку.

        Два пула с одним именем в одном `PrometheusMetrics` перетирали бы друг другу гауджи — это
        `ValueError`: назовите их (`name="mail"`, `name="shop"`).
        """
        if name in self._attached:
            msg = f"пул с именем {name!r} уже подключён к метрикам: укажите другое name="
            raise ValueError(msg)
        self._attached.add(name)
        detach = pool.on(PoolEvent, lambda event: self.observe(event, pool=name))

        def stop() -> None:
            detach()
            self._attached.discard(name)

        return stop

    def observe(self, event: PoolEvent, *, pool: str = "default") -> None:
        """Учесть одно событие пула `pool`."""
        counter = self._counted.get(type(event))
        if counter is not None:
            counter.labels(pool=pool).inc()
            return
        if type(event) in _BROWSER_EVENTS:
            self.browser_events.labels(pool=pool, event=_BROWSER_EVENTS[type(event)]).inc()
            return
        match event:
            case LeaseAcquired():
                self.lease_wait.labels(pool=pool).observe(event.waited)
            case LeaseReleased():
                outcome = event.outcome.value if event.outcome is not None else "ok"
                self.leases.labels(pool=pool, outcome=outcome).inc()
                self.lease_held.labels(pool=pool).observe(event.held)
            case TaskRetried():
                self.task_retries.labels(pool=pool, kind=event.kind).inc()
            case ResourcePressure():
                self.under_pressure.labels(pool=pool).set(1 if event.reason is not None else 0)
            case PoolHealth():
                self._health(event, pool)
            case _:
                pass

    def _health(self, event: PoolHealth, pool: str) -> None:
        snapshot = event.snapshot
        self.leases_active.labels(pool=pool).set(snapshot.leases_active)
        self.waiting.labels(pool=pool).set(snapshot.waiting)
        self.capacity.labels(pool=pool, scope="total").set(snapshot.capacity_total)
        self.capacity.labels(pool=pool, scope="healthy").set(snapshot.capacity_healthy)
        self.under_pressure.labels(pool=pool).set(1 if snapshot.under_pressure else 0)
        for state in BrowserState:
            count = sum(browser.state is state for browser in snapshot.browsers)
            self.browsers.labels(pool=pool, state=state.value).set(count)


__all__ = ["PrometheusMetrics"]
