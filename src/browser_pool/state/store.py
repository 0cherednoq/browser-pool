"""Хранилища записей identity с оптимистичной блокировкой.

`save(record)` сохраняет запись, только если в хранилище та же версия, что в `record.version`
(0 — записи ещё нет), и возвращает запись со следующей версией. Иначе — `StaleRecordError`: запись
успел изменить кто-то другой, и перезаписывать её молча нельзя.

- `MemoryStateStore` — в памяти процесса: тесты и одноразовые identity;
- `FileStateStore` — файл JSON на identity; запись атомарна (временный файл и `os.replace`),
  оборванная запись не портит сохранённое;
- `CallbackStateStore` — хранилище приложения (БД) через две-три функции; сравнение версий —
  на стороне приложения: пул передаёт ожидаемую версию.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from browser_pool._filename import safe_file_name
from browser_pool.clock import utc_now
from browser_pool.state.record import StaleRecordError, dump_record, load_record

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from browser_pool.state.record import IdentityRecord


@runtime_checkable
class StateStore(Protocol):
    """Где живут записи identity."""

    async def load(self, key: str) -> IdentityRecord | None:
        """Запись identity или `None`, если её нет."""
        ...

    async def save(self, record: IdentityRecord) -> IdentityRecord:
        """Сохранить поверх версии `record.version`; вернуть запись со следующей версией."""
        ...

    async def delete(self, key: str) -> None:
        """Удалить запись; её отсутствие — не ошибка."""
        ...


def _next(record: IdentityRecord) -> IdentityRecord:
    return replace(record, version=record.version + 1, updated_at=utc_now())


class MemoryStateStore:
    """Записи в памяти процесса."""

    def __init__(self) -> None:
        self._records: dict[str, IdentityRecord] = {}

    async def load(self, key: str) -> IdentityRecord | None:
        """Запись identity или `None`."""
        return self._records.get(key)

    async def save(self, record: IdentityRecord) -> IdentityRecord:
        """Сохранить с проверкой версии."""
        current = self._records.get(record.key)
        actual = current.version if current is not None else 0
        if actual != record.version:
            raise StaleRecordError(key=record.key, expected=record.version, actual=actual)
        saved = _next(record)
        self._records[record.key] = saved
        return saved

    async def delete(self, key: str) -> None:
        """Удалить запись."""
        self._records.pop(key, None)


_RETRIES = 6
"""Сколько раз повторить чтение или замену файла, если ОС отказала: антивирус или другой процесс держит его."""
_RETRY_PAUSE = 0.02


class FileStateStore:
    """Файл JSON на identity в каталоге `directory`.

    Имя файла — читаемая часть ключа и хеш ключа как он есть (`safe_file_name`): ключи, различающиеся
    регистром или обрезанной частью, — разные записи на любой файловой системе; `load` сверяет ключ
    записи с запрошенным. Чтение, сравнение версий и запись идут под локом процесса по ключу; на
    Windows замена файла, открытого читателем из другого процесса, повторяется несколько раз.
    Два процесса над одним каталогом друг друга не видят — для этого блокировка identity (`IdentityLock`).
    """

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}
        """Держатель и ждущие по ключу: когда их не осталось, замок ключа удаляется."""

    async def load(self, key: str) -> IdentityRecord | None:
        """Запись identity или `None`."""
        async with self._exclusive(key):
            return await asyncio.to_thread(self._read, key)

    async def save(self, record: IdentityRecord) -> IdentityRecord:
        """Сохранить с проверкой версии, атомарно."""
        async with self._exclusive(record.key):
            current = await asyncio.to_thread(self._read, record.key)
            actual = current.version if current is not None else 0
            if actual != record.version:
                raise StaleRecordError(key=record.key, expected=record.version, actual=actual)
            saved = _next(record)
            # Сначала — в текст целиком: несериализуемое падает до того, как что-то записано.
            text = json.dumps(dump_record(saved), ensure_ascii=False, indent=2)
            await asyncio.to_thread(self._write, record.key, text)
            return saved

    async def delete(self, key: str) -> None:
        """Удалить запись."""
        async with self._exclusive(key):
            await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    @contextlib.asynccontextmanager
    async def _exclusive(self, key: str) -> AsyncGenerator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
                del self._locks[key]

    def _path(self, key: str) -> Path:
        return self._directory / f"{safe_file_name(key)}.json"

    def _read(self, key: str) -> IdentityRecord | None:
        text = _read_text(self._path(key))
        if text is None:
            return None
        record = load_record(cast("dict[str, Any]", json.loads(text)))
        return record if record.key == key else None  # чужая запись под этим именем — не наша

    def _write(self, key: str, text: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._path(key)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._directory, prefix=f".{target.stem}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            _replace(Path(temporary), target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def _read_text(path: Path) -> str | None:
    """Текст файла или `None`, если его нет; на Windows отказ ОС во время замены файла — с повтором."""
    for attempt in range(_RETRIES):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_PAUSE * (attempt + 1))  # noqa: TID251 — это функция потока, не цикла
    return None


def _replace(source: Path, target: Path) -> None:
    """`os.replace` с повтором: на Windows замена файла, открытого читателем, — `PermissionError`."""
    for attempt in range(_RETRIES):
        try:
            source.replace(target)
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_PAUSE * (attempt + 1))  # noqa: TID251 — это функция потока, не цикла
        else:
            return


type LoadCallback = Callable[[str], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
type SaveCallback = Callable[[str, dict[str, Any], int], Awaitable[None] | None]
type DeleteCallback = Callable[[str], Awaitable[None] | None]


class CallbackStateStore:
    """Хранилище приложения через функции: записи ходят простыми данными (`dump_record`).

    Функции могут быть обычными или корутинными, как у `CallbackProxySource`.

    `save(key, data, expected_version)` обязана записать `data`, только если сохранённая версия
    равна `expected_version` (0 — записи нет), иначе бросить `StaleRecordError`: например,
    `UPDATE … WHERE version = :expected` и проверка числа изменённых строк.
    """

    def __init__(
        self,
        *,
        load: LoadCallback,
        save: SaveCallback,
        delete: DeleteCallback | None = None,
    ) -> None:
        self._load = load
        self._save = save
        self._delete = delete

    async def load(self, key: str) -> IdentityRecord | None:
        """Запись identity или `None`."""
        data = await _settle(self._load(key))
        return load_record(data) if data is not None else None

    async def save(self, record: IdentityRecord) -> IdentityRecord:
        """Сохранить через функцию приложения; сравнение версий — её забота."""
        saved = _next(record)
        await _settle(self._save(record.key, dump_record(saved), record.version))
        return saved

    async def delete(self, key: str) -> None:
        """Удалить запись, если приложение дало функцию удаления."""
        if self._delete is not None:
            await _settle(self._delete(key))


async def _settle[T](result: Awaitable[T] | T) -> T:
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = [
    "CallbackStateStore",
    "DeleteCallback",
    "FileStateStore",
    "LoadCallback",
    "MemoryStateStore",
    "SaveCallback",
    "StateStore",
]
