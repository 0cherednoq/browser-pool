"""Состояние сессии: значения, разбор форматов, записи identity и хранилища."""

from __future__ import annotations

from browser_pool.state.record import IdentityRecord, StaleRecordError, dump_record, load_record
from browser_pool.state.session_state import (
    Cookie,
    Origin,
    SameSite,
    SessionState,
    StateFormatError,
)
from browser_pool.state.store import (
    CallbackStateStore,
    FileStateStore,
    MemoryStateStore,
    StateStore,
)

__all__ = [
    "CallbackStateStore",
    "Cookie",
    "FileStateStore",
    "IdentityRecord",
    "MemoryStateStore",
    "Origin",
    "SameSite",
    "SessionState",
    "StaleRecordError",
    "StateFormatError",
    "StateStore",
    "dump_record",
    "load_record",
]
