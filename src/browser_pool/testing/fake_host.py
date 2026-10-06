"""Фейковые замеры хоста: память, CPU и RSS браузеров — какие скажет тест (`Resources`)."""

from __future__ import annotations


class FakeHostProbe:
    """Замеры хоста без хоста. Реализует `HostProbe`; значения меняются прямо в полях."""

    def __init__(self, *, free_memory_mb: float = 8192.0, cpu_percent: float = 10.0) -> None:
        self.free_memory: float = free_memory_mb
        """Сколько памяти «доступно», МБ."""
        self.cpu: float = cpu_percent
        """Загрузка CPU, которую вернёт следующий замер."""
        self.rss: dict[int, float] = {}
        """RSS дерева процессов по PID, МБ; нет PID — процесса «нет»."""

    def free_memory_mb(self) -> float:
        """Доступная память."""
        return self.free_memory

    def cpu_percent(self) -> float:
        """Загрузка CPU."""
        return self.cpu

    def tree_rss_mb(self, pid: int) -> float | None:
        """RSS дерева процессов."""
        return self.rss.get(pid)


__all__ = ["FakeHostProbe"]
