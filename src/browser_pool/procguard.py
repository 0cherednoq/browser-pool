"""Страж процессов: браузеры пула не переживают ни пул, ни процесс приложения.

Штатно браузер закрывает драйвер. Страж — страховка на случаи, когда этого мало:

- браузер не завершился после закрытия и добивания драйвером — убивается всё дерево его
  процессов (рендереры, GPU, утилиты);
- процесс приложения умер, не успев ничего закрыть (`kill -9`, падение, выключение), — на диске
  остаётся реестр его браузеров, и следующий старт пула на этой машине их добивает
  (`reap_orphans`).

Реестр — файл на каждый пул в `registry` (по умолчанию во временном каталоге системы, в каталоге
пользователя ОС: чужой реестр не читается и не мешает). Процесс в нём записан номером и
отпечатком — временем создания: номера процессов переиспользуются, и без отпечатка страж однажды
убил бы чужой процесс, получивший номер умершего браузера. Процесс, отпечаток которого не удалось
снять, в реестр не попадает; из реестра процесс уходит, только когда его точно нет.

Реестр — страховка, а не условие работы: не записался (файл занят, нет прав) — предупреждение в
лог, запуск и закрытие браузера идут дальше.

Только stdlib: Windows — WinAPI через `ctypes` и `taskkill`, Linux — `/proc`, прочие POSIX — `ps`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, cast

from browser_pool.clock import monotonic

_logger = logging.getLogger(__name__)


def default_registry() -> Path:
    """Каталог реестров по умолчанию — свой у каждого пользователя ОС.

    Временный каталог на POSIX общий (`/tmp`): в общем каталоге реестров второй пользователь хоста
    не смог бы ни записать свой реестр, ни прибрать чужой, а подложенный файл указал бы стражу,
    кого убить. На Windows временный каталог и так принадлежит пользователю.
    """
    owner = "" if sys.platform == "win32" else f"-{os.getuid()}"
    return Path(tempfile.gettempdir()) / f"browser_pool{owner}" / "procguard"


DEFAULT_REGISTRY = default_registry()
"""Где пулы пользователя на этой машине записывают свои браузеры."""

_STEP = 0.1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_STAT_START_TIME = 19
"""Поле starttime в /proc/<pid>/stat, считая после `(comm)`."""
_ZOMBIE = "Z"


class ProcessGuard:
    """Реестр процессов браузеров пула и их добивание."""

    def __init__(self, registry: Path | None = None) -> None:
        """`registry` — каталог реестров; общий для всех пулов машины, которые должны видеть сирот друг друга."""
        self._dir = registry if registry is not None else DEFAULT_REGISTRY
        self._file = self._dir / f"pool-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        self._tracked: dict[int, str] = {}
        self._writing = threading.Lock()
        """Запись реестра — по одной: браузеры пула запускаются и закрываются разом."""

    @property
    def tracked(self) -> frozenset[int]:
        """Процессы, которые страж сейчас держит на учёте."""
        return frozenset(self._tracked)

    async def track(self, pid: int) -> None:
        """Взять процесс браузера на учёт."""
        token = await asyncio.to_thread(process_token, pid)
        if token is None:
            _logger.info("Процесс %d не найден — на учёт не взят", pid)
            return
        self._tracked[pid] = token
        await asyncio.to_thread(self._write)

    async def release(self, pid: int, *, grace: float) -> bool:
        """Браузер закрыт: дать процессу `grace` секунд завершиться, иначе убить дерево. `True` — пришлось убить."""
        token = self._tracked.get(pid)
        if token is None:
            return False
        killed = await self._wait_gone(pid, token, grace=grace)
        # Из реестра — только теперь: упади приложение раньше, сироту найдёт следующий запуск.
        self._tracked.pop(pid, None)
        await asyncio.to_thread(self._write)
        return killed

    async def _wait_gone(self, pid: int, token: str, *, grace: float) -> bool:
        deadline = monotonic() + grace
        while await asyncio.to_thread(process_token, pid) == token:
            if monotonic() >= deadline:
                _logger.warning(
                    "Процесс браузера %d не завершился после закрытия — убиваю дерево", pid
                )
                await asyncio.to_thread(kill_tree, pid)
                return True
            await asyncio.sleep(_STEP)
        return False

    async def reap_orphans(self) -> int:
        """Добить браузеры пулов, чьи процессы умерли. Сколько процессов добито."""
        return await asyncio.to_thread(self._reap)

    def close(self) -> None:
        """Пул остановлен: реестр больше не нужен."""
        self._tracked.clear()
        with self._writing, contextlib.suppress(OSError):
            self._file.unlink(missing_ok=True)

    # --- внутреннее --------------------------------------------------------------------

    def _write(self) -> None:
        """Записать реестр как он есть сейчас. Не бросает: реестр — страховка, а не условие работы."""
        with self._writing:
            # Состояние читается под замком: кто пишет последним, пишет самое свежее.
            tracked = dict(self._tracked)
            try:
                self._store(tracked)
            except OSError as error:
                _logger.warning(
                    "Реестр браузеров пула не записан (%s): если приложение упадёт, следующий запуск "
                    "не найдёт осиротевшие браузеры",
                    type(error).__name__,
                )

    def _store(self, tracked: dict[int, str]) -> None:
        if not tracked:
            self._file.unlink(missing_ok=True)
            return
        if not _own_directory(self._dir):
            msg = f"каталог реестра {self._dir} принадлежит другому пользователю"
            raise PermissionError(msg)
        owner = os.getpid()
        record = {
            "owner": {"pid": owner, "token": process_token(owner)},
            "processes": [{"pid": pid, "token": token} for pid, token in tracked.items()],
        }
        temporary = self._file.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(record), encoding="utf-8")
            temporary.replace(self._file)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise

    def _reap(self) -> int:
        """Сироты чужих реестров. Не бросает: реестр, который не читается или не удаляется, пропускается."""
        try:
            if not self._dir.is_dir() or not _own_directory(self._dir):
                return 0
            paths = sorted(self._dir.glob("pool-*.json"))
        except OSError:
            return 0
        reaped = 0
        for path in paths:
            if path == self._file:
                continue
            record = _read(path)
            if record is not None and _alive(record.get("owner")):
                continue
            for entry in cast("list[Any]", (record or {}).get("processes", [])):
                if _alive(entry):
                    _logger.warning("Добиваю осиротевший браузер %s прошлого запуска", entry["pid"])
                    kill_tree(int(entry["pid"]))
                    reaped += 1
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        return reaped


def _own_directory(directory: Path) -> bool:
    """Каталог реестра создан (или создаётся) этим пользователем и закрыт от остальных."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform == "win32":
        return True
    return directory.stat().st_uid == os.getuid()


def parse_proc_stat(text: str) -> tuple[str, int, str] | None:
    """Состояние, родитель и время создания из `/proc/<pid>/stat`; `None` — строка не разобралась.

    Имя процесса в скобках может содержать и пробелы, и скобки — поля считаются после последней `)`.
    """
    fields = text.rpartition(")")[2].split()
    if len(fields) <= _STAT_START_TIME or not fields[1].isdigit():
        return None
    return fields[0], int(fields[1]), fields[_STAT_START_TIME]


def process_token(pid: int) -> str | None:
    """Отпечаток живого процесса — время его создания. `None` — процесса нет."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_token(pid)
    if _has_proc():
        try:
            stat = parse_proc_stat(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
        except OSError:
            return None
        # Зомби уже мёртв: ждать его и убивать незачем.
        return None if stat is None or stat[0] == _ZOMBIE else stat[2]
    return _ps_token(pid)


def _has_proc() -> bool:
    return Path("/proc/self/stat").exists()


def kill_tree(pid: int) -> None:
    """Убить процесс и всех его потомков. Процесса уже нет — не ошибка."""
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run(  # noqa: S603 — полный путь, аргументы — числа
            [str(taskkill), "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
        )
        return
    for victim in [*_descendants(pid), pid]:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(victim, signal.SIGKILL)


def _alive(entry: object) -> bool:
    """Жив ли процесс из реестра — тот же самый, а не новый с его номером."""
    if not isinstance(entry, dict):
        return False
    data = cast("dict[str, Any]", entry)
    pid, token = data.get("pid"), data.get("token")
    return isinstance(pid, int) and token is not None and process_token(pid) == token


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _windows_token(pid: int) -> str | None:
    if sys.platform != "win32":  # сужение платформы для типизатора: windll есть только здесь
        return None
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)  # noqa: FBT003 — WinAPI
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        if code.value != _STILL_ACTIVE:
            return None
        created, exited, kernel_time, user_time = (ctypes.c_ulonglong() for _ in range(4))
        ok = kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        return str(created.value) if ok else None
    finally:
        kernel.CloseHandle(handle)


def _ps_token(pid: int) -> str | None:
    ps = shutil.which("ps")
    if ps is None:
        return None
    result = subprocess.run(  # noqa: S603 — путь из which, аргументы — числа
        [ps, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    token = result.stdout.strip()
    return token or None


def _descendants(pid: int) -> list[int]:
    """Потомки процесса, глубокие — первыми: дети не успеют переродиться у init."""
    children = _children_from_proc() if _has_proc() else _children_from_ps()
    ordered: list[int] = []
    stack = list(children.get(pid, []))
    while stack:
        current = stack.pop()
        ordered.append(current)
        stack.extend(children.get(current, []))
    return ordered[::-1]


def _children_from_proc() -> dict[int, list[int]]:
    """Дети по родителям из `/proc` — без `ps`, которого в тонких образах нет."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = parse_proc_stat((entry / "stat").read_text(encoding="utf-8"))
        except OSError:
            continue  # процесс успел завершиться
        if stat is not None:
            children.setdefault(stat[1], []).append(int(entry.name))
    return children


def _children_from_ps() -> dict[int, list[int]]:
    ps = shutil.which("ps")
    if ps is None:
        return {}
    result = subprocess.run(  # noqa: S603 — путь из which
        [ps, "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True, check=False
    )
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):  # noqa: PLR2004 — pid и ppid
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return children


__all__ = [
    "DEFAULT_REGISTRY",
    "ProcessGuard",
    "default_registry",
    "kill_tree",
    "parse_proc_stat",
    "process_token",
]
