"""Эксклюзивность identity: один аккаунт — один открытый контекст на всех воркерах.

Внутри одного пула это гарантирует планировщик. Замок нужен, когда identity могут открыть
несколько пулов — в одном процессе, в нескольких или на нескольких машинах: сайты разлогинивают
аккаунт, вошедший дважды. Пул берёт замок перед открытием контекста и отпускает после его
закрытия; не дождался за `Timeouts.open` — `IdentityBusyError`, identity уходит на паузу.

По умолчанию — `LocalIdentityLock`: замок на процесс, общий для всех пулов, которым его передали.
Для нескольких процессов на одном хосте — `FileIdentityLock`; для нескольких машин приложение
даёт свой: advisory lock PostgreSQL, Redis.

`FileLock` — замок на файл, который держит ОС (`flock` / `LockFileEx` через `msvcrt`): он общий для
всех процессов хоста и снимается сам, когда процесс умирает, даже от `kill -9`. Им же пул
защищает профиль на диске (`StatePolicy.user_data_dir`) на всё время жизни его браузера.
"""

from __future__ import annotations

import asyncio
import errno
import os
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from browser_pool._filename import safe_file_name

_BUSY = frozenset(
    code
    for code in (
        errno.EACCES,
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)
"""Коды отказа «замок занят» у `flock` и `msvcrt.locking`; остальные — ФС блокировок не поддерживает."""

_POLL = 0.1
"""Как часто пробовать занятый файловый замок, секунды: ОС не даёт асинхронного ожидания."""


@runtime_checkable
class IdentityLock(Protocol):
    """Замок identity. `acquire` ждёт, пока identity не освободится."""

    async def acquire(self, key: str) -> None:
        """Взять identity; ждать, если она занята."""
        ...

    async def release(self, key: str) -> None:
        """Отпустить identity."""
        ...


class LocalIdentityLock:
    """Замок в памяти процесса: identity занимает один пул из тех, что делят этот замок."""

    def __init__(self) -> None:
        """Пустой замок."""
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}
        """Держатель и ждущие: когда их не осталось, замок identity удаляется."""

    def held(self, key: str) -> bool:
        """Занята ли identity сейчас."""
        lock = self._locks.get(key)
        return lock is not None and lock.locked()

    async def acquire(self, key: str) -> None:
        """Взять identity; ждать, если она занята."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            await lock.acquire()
        except BaseException:
            self._forget(key)
            raise

    async def release(self, key: str) -> None:
        """Отпустить identity. Не взятую — нарушение: значит, отпускают чужое."""
        lock = self._locks.get(key)
        if lock is None or not lock.locked():
            msg = f"identity {key} не занята — отпускать нечего"
            raise RuntimeError(msg)
        lock.release()
        self._forget(key)

    def _forget(self, key: str) -> None:
        self._users[key] -= 1
        if not self._users[key]:
            del self._users[key]
            del self._locks[key]


class FileLock:
    """Замок на файл `path`: один держатель на весь хост; умер процесс — замок свободен.

    Держатель — этот объект, а не процесс: второй `FileLock` на тот же файл в том же процессе
    тоже ждёт. Файл замка не удаляется при отпускании — иначе два процесса могли бы держать
    замки на разные файлы с одним именем.
    """

    def __init__(self, path: Path | str, *, poll_interval: float = _POLL) -> None:
        """Замок ещё не взят; каталог файла создаётся при первой попытке."""
        self.path: Path = Path(path)
        self._poll = poll_interval
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """Взят ли замок этим объектом."""
        return self._fd is not None

    async def acquire(self) -> None:
        """Взять замок; ждать, пока его держит кто-то другой. Срок ожидания — у вызывающего."""
        if self._fd is not None:
            msg = f"Замок {self.path} уже взят этим держателем"
            raise RuntimeError(msg)
        while not await self._attempt():  # noqa: ASYNC110 — события «файл отпущен» ОС не даёт
            await asyncio.sleep(self._poll)

    def release(self) -> None:
        """Отпустить замок. Не взятый — нарушение: значит, отпускают чужое."""
        fd, self._fd = self._fd, None
        if fd is None:
            msg = f"Замок {self.path} не взят — отпускать нечего"
            raise RuntimeError(msg)
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    async def _attempt(self) -> bool:
        """Одна попытка — в потоке. Отмена посреди попытки не оставляет взятого замка."""
        attempt = asyncio.ensure_future(asyncio.to_thread(self._try_lock))
        try:
            fd = await asyncio.shield(attempt)
        except asyncio.CancelledError:
            fd = await attempt
            if fd is not None:
                _unlock(fd)
                os.close(fd)
            raise
        self._fd = fd
        return fd is not None

    def _try_lock(self) -> int | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError as error:
            os.close(fd)
            if error.errno in _BUSY:
                return None
            # Не «занято», а «не умею»: сетевая или иная ФС без блокировок. Ждать бессмысленно.
            msg = (
                f"Файловая система не поддерживает блокировки файлов ({self.path}): "
                f"{error.strerror or type(error).__name__}"
            )
            raise OSError(error.errno, msg) from None
        return fd


class FileIdentityLock:
    """Замок identity на файлах каталога `directory`: для нескольких процессов на одном хосте.

    На каждую identity — файл `<ключ>.lock` (имя из ключа, безопасное для файловой системы).
    Процесс-держатель упал — identity свободна сразу, без сроков аренды и уборки.
    """

    def __init__(self, directory: Path | str, *, poll_interval: float = _POLL) -> None:
        """Каталог создаётся при первом захвате."""
        self.directory: Path = Path(directory)
        self._poll = poll_interval
        self._local = LocalIdentityLock()
        """Очередь своих ждущих: к файлу идёт один, остальные ждут без опроса."""
        self._held: dict[str, FileLock] = {}

    def held(self, key: str) -> bool:
        """Занята ли identity этим замком."""
        return key in self._held

    async def acquire(self, key: str) -> None:
        """Взять identity; ждать, если её держит этот или другой процесс."""
        await self._local.acquire(key)
        lock = FileLock(self.directory / f"{safe_file_name(key)}.lock", poll_interval=self._poll)
        try:
            await lock.acquire()
        except BaseException:
            await self._local.release(key)
            raise
        self._held[key] = lock

    async def release(self, key: str) -> None:
        """Отпустить identity. Не взятую — нарушение: значит, отпускают чужое."""
        lock = self._held.pop(key, None)
        if lock is None:
            msg = f"identity {key} не занята — отпускать нечего"
            raise RuntimeError(msg)
        try:
            lock.release()
        finally:
            await self._local.release(key)


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        """Эксклюзивный замок без ожидания; занят — `OSError`."""
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        """Эксклюзивный замок без ожидания; занят — `OSError`."""
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = ["FileIdentityLock", "FileLock", "IdentityLock", "LocalIdentityLock", "safe_file_name"]
