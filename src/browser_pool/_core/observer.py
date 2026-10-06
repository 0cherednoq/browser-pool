"""Наблюдение за пулом: счётчики, снимок, сторож долгих аренд.

Фасад сообщает сюда, что произошло, а отсюда уходят события на шину. Снимок собирается из
логического учёта планировщика, физических ресурсов и супервизора в один момент — между `await`
он согласован сам собой.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from browser_pool.clock import monotonic
from browser_pool.errors import LeaseRevokedError
from browser_pool.events import LeakSuspected, LeaseRevoked
from browser_pool.snapshot import (
    BrowserSnapshot,
    BrowserState,
    ContextSnapshot,
    IdentitySnapshot,
    PoolCounters,
    PoolSnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from browser_pool._core.contexts import PhysicalResources
    from browser_pool._core.scheduler import Grant, Scheduler
    from browser_pool.config import Limits
    from browser_pool.events import PoolEvent

_logger = logging.getLogger(__name__)
_STACK_DEPTH = 16


@dataclass(slots=True)
class _Counters:
    """Изменяемые счётчики; наружу уходят снимком `PoolCounters`."""

    acquired: int = 0
    wait_total: float = 0.0
    wait_max: float = 0.0
    acquire_timeouts: int = 0
    contexts_opened: int = 0
    contexts_closed: int = 0
    open_failures: int = 0


class LeaseWatch:
    """Слежка за одной арендой: таймеры сторожа и то, чем кончился отзыв.

    Отзыв отменяет задачу арендатора, как `asyncio.timeout`: на выходе из аренды отмена, вызванная
    только отзывом, превращается в `LeaseRevokedError`, а задача остаётся неотменённой. Если задачу
    отменили ещё и снаружи, отмена остаётся отменой.
    """

    def __init__(self, grant: Grant, holder: asyncio.Task[Any] | None) -> None:
        self._grant = grant
        self._holder = holder
        self._cancelling = holder.cancelling() if holder is not None else 0
        self._settled = False
        self.handles: list[asyncio.TimerHandle] = []
        self.revoked_after: float | None = None
        """Сколько секунд держалась аренда на момент отзыва; `None` — не отзывалась."""

    def revoke(self, held: float) -> None:
        """Отозвать аренду: отменить задачу арендатора."""
        if self._holder is None:
            return
        self.revoked_after = held
        self._holder.cancel(f"аренда {self._grant.lease_id} дольше lease_max_duration")

    def revoked(self, error: BaseException) -> LeaseRevokedError | None:
        """Ошибка пула вместо отмены, если отмену вызвал отзыв, и только он; иначе `None`."""
        if self.revoked_after is None or self._settled or self._holder is None:
            return None
        self._settled = True
        ours = self._holder.uncancel() <= self._cancelling
        if not ours or not isinstance(error, asyncio.CancelledError):
            return None
        return LeaseRevokedError(
            lease_id=self._grant.lease_id,
            identity=self._grant.identity.key,
            held=self.revoked_after,
        )

    def stop(self) -> None:
        """Аренда возвращается: снять таймеры; отзыв, который арендатор проглотил, — погасить."""
        for handle in self.handles:
            handle.cancel()
        if self.revoked_after is not None and not self._settled and self._holder is not None:
            self._settled = True
            self._holder.uncancel()


class Observer:
    """Счётчики и снимок пула; сторож долгих аренд."""

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        resources: PhysicalResources[Any, Any, Any],
        limits: Limits,
        emit: Callable[[PoolEvent], None],
        restarts: Callable[[], int],
        pressure: Callable[[], str | None] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._resources = resources
        self._limits = limits
        self._emit = emit
        self._restarts = restarts
        self._pressure = pressure
        self.counters: _Counters = _Counters()

    def reconfigure(self, limits: Limits) -> None:
        """Новые пороги сторожа аренд — для следующих проверок."""
        self._limits = limits

    # --- счёт --------------------------------------------------------------------------

    def acquired(self, waited: float) -> None:
        """Аренда выдана после `waited` секунд ожидания."""
        self.counters.acquired += 1
        self.counters.wait_total += waited
        self.counters.wait_max = max(self.counters.wait_max, waited)

    # --- сторож аренд ------------------------------------------------------------------

    def watch(self, grant: Grant) -> LeaseWatch:
        """Следить за арендой: долгая — событие со стеком захвата, слишком долгая — отзыв.

        Слежку снимает `LeaseWatch.stop()` при возврате аренды.
        """
        loop = asyncio.get_running_loop()
        started = monotonic()
        watch = LeaseWatch(grant, asyncio.current_task())
        warn_after = self._limits.leak_warn_after
        if warn_after is not None:
            # Стек — сейчас: потом места захвата уже не восстановить.
            stack = "".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1])
            watch.handles.append(loop.call_later(warn_after, self._suspect, grant, started, stack))
        revoke_after = self._limits.lease_max_duration
        if revoke_after is not None:
            watch.handles.append(loop.call_later(revoke_after, self._revoke, grant, started, watch))
        return watch

    def _suspect(self, grant: Grant, started: float, stack: str) -> None:
        held = monotonic() - started
        _logger.warning(
            "Аренда %d (%s) держится %.0f с — возможно, её забыли вернуть",
            grant.lease_id,
            grant.identity.key,
            held,
            extra={"lease_id": grant.lease_id, "browser_id": grant.browser_id},
        )
        self._emit(
            LeakSuspected(
                lease_id=grant.lease_id, key=grant.identity.key, held=held, acquired_at=stack
            )
        )

    def _revoke(self, grant: Grant, started: float, watch: LeaseWatch) -> None:
        held = monotonic() - started
        _logger.error(
            "Аренда %d (%s) отозвана: держится %.0f с, предел — lease_max_duration",
            grant.lease_id,
            grant.identity.key,
            held,
            extra={"lease_id": grant.lease_id, "browser_id": grant.browser_id},
        )
        self._emit(LeaseRevoked(lease_id=grant.lease_id, key=grant.identity.key, held=held))
        watch.revoke(held)

    # --- снимок ------------------------------------------------------------------------

    def snapshot(self) -> PoolSnapshot:
        """Согласованный снимок пула без секретов."""
        now = monotonic()
        browsers = self._scheduler.browsers()
        waiting = self._scheduler.waiting()
        oldest = min(waiting, key=lambda waiter: waiter.enqueued_at, default=None)
        counters = self.counters
        return PoolSnapshot(
            pressure=self._pressure() if self._pressure is not None else None,
            capacity_total=sum(view.capacity for view in browsers),
            capacity_healthy=sum(
                view.capacity for view in browsers if view.state is BrowserState.healthy
            ),
            leases_active=self._scheduler.active_leases,
            waiting=len(waiting),
            oldest_waiter_seconds=now - oldest.enqueued_at if oldest is not None else 0.0,
            oldest_waiter_candidates=(
                tuple(identity.key for identity in oldest.candidates) if oldest is not None else ()
            ),
            browsers=tuple(
                BrowserSnapshot(
                    id=view.id,
                    state=view.state,
                    launched=self._resources.launched(view.id) is not None,
                    capacity=view.capacity,
                    active=view.active,
                    contexts=view.contexts,
                    leases_total=view.leases_total,
                )
                for view in browsers
            ),
            contexts=tuple(
                ContextSnapshot(
                    key=view.key,
                    browser_id=view.browser_id,
                    generation=view.generation,
                    active=view.active,
                    limit=view.limit,
                    retiring=view.retiring,
                    labels=view.labels,
                )
                for view in self._scheduler.contexts()
            ),
            identities=tuple(
                IdentitySnapshot(
                    key=status.key,
                    cooling_for=(
                        status.cooling_until - now if status.cooling_until is not None else None
                    ),
                    blocked=status.blocked,
                    open_failures=status.open_failures,
                )
                for status in self._scheduler.identity_statuses(now=now)
            ),
            counters=PoolCounters(
                acquired=counters.acquired,
                wait_total=counters.wait_total,
                wait_max=counters.wait_max,
                acquire_timeouts=counters.acquire_timeouts,
                contexts_opened=counters.contexts_opened,
                contexts_closed=counters.contexts_closed,
                open_failures=counters.open_failures,
                variant_switches=self._scheduler.variant_switches,
                restarts=self._restarts(),
                close_failures=self._resources.close_failures,
            ),
        )


__all__ = ["LeaseWatch", "Observer"]
