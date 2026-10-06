"""Замеры хоста для защиты от перегрузки (`Resources`).

Ядро знает только этот протокол; реализация на psutil — `browser_pool.monitors.psutil`
(extra `[resources]`), в тестах — `browser_pool.testing.FakeHostProbe`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class HostProbe(Protocol):
    """Замеры хоста. Методы синхронные и быстрые; пул зовёт их в потоке исполнителя."""

    def free_memory_mb(self) -> float:
        """Сколько памяти хоста доступно новым процессам, МБ."""
        ...

    def cpu_percent(self) -> float:
        """Средняя загрузка всех процессоров с прошлого вызова, 0–100."""
        ...

    def tree_rss_mb(self, pid: int) -> float | None:
        """Резидентная память процесса вместе с потомками, МБ; процесса нет — `None`."""
        ...


__all__ = ["HostProbe"]
