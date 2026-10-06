"""Давление на хост: память и CPU (`Resources`).

Сторож замеряет хост на каждой проверке здоровья и отвечает на два вопроса:

- под давлением ли хост — свободной памяти меньше порога или средняя загрузка CPU за окно
  выше порога. Пока да, пул не растёт: новых браузеров и контекстов нет, выданные аренды
  работают, ожидающие ждут;
- какие браузеры распухли — RSS дерева процессов выше порога: их перезапускают с дренажом.

Решения принимает супервизор; сторож только считает. Замеры — `HostProbe`, в потоке исполнителя.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from browser_pool.clock import monotonic

if TYPE_CHECKING:
    from collections.abc import Mapping

    from browser_pool.config import Resources
    from browser_pool.host import HostProbe


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceReport:
    """Итог одного замера."""

    pressure: str | None
    """Что давит; `None` — давления нет."""
    changed: bool
    """Давление появилось или ушло с прошлого замера."""
    heavy: tuple[str, ...]
    """Браузеры, распухшие сверх `max_browser_rss_mb`."""
    free_memory_mb: float | None
    cpu_percent: float | None


class ResourceGuard:
    """Замеры хоста и решение «давит или нет»."""

    def __init__(self, config: Resources, probe: HostProbe | None) -> None:
        self._config = config
        self._probe = probe
        self._cpu: deque[tuple[float, float]] = deque()
        self.pressure: str | None = None
        """Что давит сейчас (по последнему замеру)."""

    @property
    def active(self) -> bool:
        """Пороги заданы и замерять есть чем."""
        return self._config.active and self._probe is not None

    async def sample(self, pids: Mapping[str, int]) -> ResourceReport:
        """Замерить хост и браузеры (`pids` — PID процесса браузера по номеру в пуле)."""
        probe, config = self._probe, self._config
        if probe is None or not config.active:
            return ResourceReport(
                pressure=None, changed=False, heavy=(), free_memory_mb=None, cpu_percent=None
            )
        free = (
            await asyncio.to_thread(probe.free_memory_mb)
            if config.min_free_memory_mb is not None
            else None
        )
        cpu = await self._cpu_average(probe) if config.max_cpu_percent is not None else None
        reasons: list[str] = []
        minimum, ceiling = config.min_free_memory_mb, config.max_cpu_percent
        if free is not None and minimum is not None and free < minimum:
            reasons.append(f"свободной памяти {free:.0f} МБ < {config.min_free_memory_mb:g}")
        if cpu is not None and ceiling is not None and cpu > ceiling:
            reasons.append(f"CPU {cpu:.0f}% > {config.max_cpu_percent:g}%")
        pressure = "; ".join(reasons) or None
        changed = (pressure is None) != (self.pressure is None)
        self.pressure = pressure
        return ResourceReport(
            pressure=pressure,
            changed=changed,
            heavy=await self._heavy(probe, pids),
            free_memory_mb=free,
            cpu_percent=cpu,
        )

    async def _cpu_average(self, probe: HostProbe) -> float:
        """Средняя загрузка за окно: замеры старше `sample_window` выбрасываются."""
        now = monotonic()
        self._cpu.append((now, await asyncio.to_thread(probe.cpu_percent)))
        while self._cpu and self._cpu[0][0] < now - self._config.sample_window:
            self._cpu.popleft()
        return sum(value for _, value in self._cpu) / len(self._cpu)

    async def _heavy(self, probe: HostProbe, pids: Mapping[str, int]) -> tuple[str, ...]:
        limit = self._config.max_browser_rss_mb
        if limit is None:
            return ()
        heavy: list[str] = []
        for browser_id, pid in pids.items():
            rss = await asyncio.to_thread(probe.tree_rss_mb, pid)
            if rss is not None and rss > limit:
                heavy.append(browser_id)
        return tuple(heavy)


__all__ = ["ResourceGuard", "ResourceReport"]
