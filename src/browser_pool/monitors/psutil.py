"""`HostProbe` на psutil (extra `[resources]`): память, CPU, память дерева процессов браузера."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psutil import Process

    from browser_pool.host import HostProbe

_MB = 1024 * 1024


class PsutilProbe:
    """Замеры хоста через psutil. Реализует `HostProbe`."""

    def __init__(self) -> None:
        import psutil

        self._psutil = psutil
        psutil.cpu_percent(interval=None)  # первый вызов psutil всегда 0.0 — задать точку отсчёта

    def free_memory_mb(self) -> float:
        """Доступная память (`available`, а не `free`: кэш ОС отдаётся по требованию)."""
        return self._psutil.virtual_memory().available / _MB

    def cpu_percent(self) -> float:
        """Загрузка всех процессоров с прошлого вызова."""
        return float(self._psutil.cpu_percent(interval=None))

    def tree_rss_mb(self, pid: int) -> float | None:
        """Память браузера и всех его потомков: рендереры, GPU, утилиты.

        Общие страницы (Chrome делит их между процессами) считаются один раз: PSS на Linux, USS на
        Windows и macOS. Нет прав на подробный замер — берётся RSS этого процесса.
        """
        psutil = self._psutil
        try:
            root = psutil.Process(pid)
            processes = [root, *root.children(recursive=True)]
        except psutil.Error:
            return None
        total = 0
        for process in processes:
            with contextlib.suppress(psutil.Error):  # потомок успел завершиться
                total += _unique_bytes(process)
        return total / _MB


def _unique_bytes(process: Process) -> int:
    """Память процесса без общих страниц; недоступна — RSS."""
    try:
        full = process.memory_full_info()
    except Exception:  # noqa: BLE001 — `psutil.AccessDenied` и родня: узнать подробно не дали
        return int(process.memory_info().rss)
    unique = getattr(full, "pss", None) or getattr(full, "uss", None)
    return int(unique) if unique else int(process.memory_info().rss)


def default_probe() -> HostProbe | None:
    """`PsutilProbe`, если psutil установлен; иначе `None`."""
    try:
        return PsutilProbe()
    except ImportError:
        return None


__all__ = ["PsutilProbe", "default_probe"]
