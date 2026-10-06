"""Снимок пула для наблюдения: состояния и счётчики без секретов.

Модуль в слое модели: его читают и ядро, и приложение. В снимке — только ключи identity,
номера, состояния и числа: ни payload, ни прокси, ни кук.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BrowserState(StrEnum):
    """Где браузер в своём жизненном цикле. Новые аренды получает только `healthy`."""

    stopped = "stopped"
    """Процесса нет."""
    starting = "starting"
    """Запускается."""
    healthy = "healthy"
    """Работает и принимает новые аренды."""
    draining = "draining"
    """Плановый перезапуск или закрытие по простою: новых аренд не берёт, занятые дорабатывают."""
    quarantined = "quarantined"
    """Умер или завис: выведен из работы целиком, ждёт восстановления."""
    restarting = "restarting"
    """Восстанавливается после карантина."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserSnapshot:
    """Браузер пула."""

    id: str
    state: BrowserState
    launched: bool
    """Запущен ли процесс сейчас: здоровый браузер без процесса запустится по первой аренде."""
    capacity: int
    active: int
    """Вкладок в аренде."""
    contexts: int
    leases_total: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSnapshot:
    """Контекст identity."""

    key: str
    browser_id: str
    generation: int
    active: int
    limit: int
    retiring: bool
    labels: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityStatus:
    """Статус identity в пуле, независимый от её контекста."""

    key: str
    cooling_until: float | None
    """До какого момента монотонных часов identity на паузе; `None` — не на паузе."""
    blocked: str | None
    """Почему заблокирована; `None` — не заблокирована."""
    open_failures: int
    """Неудачных открытий подряд — от них растёт пауза."""


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentitySnapshot:
    """Identity с особым статусом: на паузе, заблокированная или после неудачных открытий."""

    key: str
    cooling_for: float | None
    """Сколько секунд паузы осталось; `None` — не на паузе."""
    blocked: str | None
    open_failures: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PoolCounters:
    """Счётчики с момента создания пула."""

    acquired: int
    wait_total: float
    """Суммарное ожидание выдачи, секунды."""
    wait_max: float
    acquire_timeouts: int
    contexts_opened: int
    contexts_closed: int
    open_failures: int
    variant_switches: int
    restarts: int
    close_failures: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PoolSnapshot:
    """Согласованный снимок пула в один момент."""

    capacity_total: int
    """Сколько вкладок пул может держать в аренде при всех здоровых браузерах."""
    capacity_healthy: int
    """То же — по здоровым браузерам сейчас."""
    leases_active: int
    waiting: int
    oldest_waiter_seconds: float
    """Сколько ждёт самая давняя заявка; 0 — очереди нет."""
    oldest_waiter_candidates: tuple[str, ...]
    browsers: tuple[BrowserSnapshot, ...]
    contexts: tuple[ContextSnapshot, ...]
    identities: tuple[IdentitySnapshot, ...]
    counters: PoolCounters
    pressure: str | None = None
    """Что давит на хост (`Resources`): пока не `None`, новые браузеры и контексты не открываются."""

    @property
    def under_pressure(self) -> bool:
        """Хост под давлением памяти или CPU."""
        return self.pressure is not None


__all__ = [
    "BrowserSnapshot",
    "BrowserState",
    "ContextSnapshot",
    "IdentitySnapshot",
    "IdentityStatus",
    "PoolCounters",
    "PoolSnapshot",
]
