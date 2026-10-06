"""Оркестрация пула браузеров: контексты аккаунтов, сессии, прокси, жизненный цикл.

Ядро не зависит ни от одной браузерной библиотеки: всё, что знает про конкретный SDK,
живёт в `browser_pool.drivers` и `browser_pool.providers`.
"""

from __future__ import annotations

from importlib.metadata import version as _version

from browser_pool.config import (
    Backoff,
    Debug,
    GroupLimit,
    Lifecycle,
    Limits,
    PoolConfig,
    Recovery,
    Recycling,
    Resources,
    Timeouts,
    Topology,
    Windows,
)
from browser_pool.errors import ErrorKind, PoolError, PoolSignal
from browser_pool.flow import BaseFlow, OpenRequest, SessionFlow
from browser_pool.geometry import Geolocation, Rect, Viewport
from browser_pool.identity import ContextOptions, Identity, ProxyPolicy, StatePolicy
from browser_pool.lease import ContextLease, PageLease
from browser_pool.locks import FileIdentityLock, IdentityLock, LocalIdentityLock
from browser_pool.pool import BrowserPool
from browser_pool.procguard import ProcessGuard
from browser_pool.proxies import Proxy

__version__: str = _version("browser-pool")
"""Версия установленного пакета — из его метаданных (единственный источник — `pyproject.toml`)."""

__all__ = [
    "Backoff",
    "BaseFlow",
    "BrowserPool",
    "ContextLease",
    "ContextOptions",
    "Debug",
    "ErrorKind",
    "FileIdentityLock",
    "Geolocation",
    "GroupLimit",
    "Identity",
    "IdentityLock",
    "Lifecycle",
    "Limits",
    "LocalIdentityLock",
    "OpenRequest",
    "PageLease",
    "PoolConfig",
    "PoolError",
    "PoolSignal",
    "ProcessGuard",
    "Proxy",
    "ProxyPolicy",
    "Recovery",
    "Rect",
    "Recycling",
    "Resources",
    "SessionFlow",
    "StatePolicy",
    "Timeouts",
    "Topology",
    "Viewport",
    "Windows",
]
