"""Граница публичного API: что поддерживается, а что внутреннее (M7.07).

Тест фиксирует контракт: новое имя в корневом `__all__` или публичном методе пула — осознанное
решение (правится этот список), а не побочный эффект. Внутренности живут в `_core` и не попадают
в `__all__` публичных модулей.
"""

from __future__ import annotations

import importlib
import pkgutil

import browser_pool
from browser_pool import BrowserPool

ROOT_API = {
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
    "Recycling",
    "Rect",
    "Resources",
    "SessionFlow",
    "StatePolicy",
    "Timeouts",
    "Topology",
    "Viewport",
    "Windows",
}

POOL_API = {
    "block",
    "config",
    "context",
    "cool_down",
    "effective_config",
    "export_state",
    "identity_status",
    "map",
    "on",
    "page",
    "reconfigure",
    "resize",
    "run",
    "snapshot",
    "start",
    "stop",
    "terminate",
    "unblock",
}

INTERNAL = {
    "EventBus",
    "HookRunner",
    "LeaseControl",
    "PhysicalResources",
    "Scheduler",
    "Supervisor",
}

STABLE_SUBPACKAGES = (
    "browser_pool.drivers",
    "browser_pool.providers",
    "browser_pool.proxies",
    "browser_pool.state",
    "browser_pool.events",
    "browser_pool.testing",
)


def public_modules() -> list[str]:
    names: list[str] = []
    for info in pkgutil.walk_packages(browser_pool.__path__, "browser_pool."):
        if any(part.startswith("_") for part in info.name.split(".")):
            continue
        names.append(info.name)
    return names


def test_root_exports_exactly_the_supported_names() -> None:
    assert set(browser_pool.__all__) == ROOT_API
    for name in ROOT_API:
        assert hasattr(browser_pool, name), name


def test_pool_has_no_lease_plumbing_in_its_public_api() -> None:
    public = {name for name in dir(BrowserPool) if not name.startswith("_")}
    assert public == POOL_API


def test_internal_classes_are_not_exported_by_public_modules() -> None:
    for name in public_modules():
        if name.endswith(("drivers.playwright", "drivers.pydoll", "monitors.prometheus")):
            continue  # нужны экстры SDK; внутренностей ядра в них нет
        module = importlib.import_module(name)
        leaked = INTERNAL & set(getattr(module, "__all__", ()))
        assert not leaked, f"{name} экспортирует внутреннее: {sorted(leaked)}"


def test_core_is_private() -> None:
    assert not any(name.startswith("browser_pool.core") for name in public_modules())
    assert not hasattr(browser_pool, "core")
    assert importlib.import_module("browser_pool._core")


def test_stable_subpackages_are_importable_and_documented() -> None:
    for name in STABLE_SUBPACKAGES:
        module = importlib.import_module(name)
        assert module.__doc__, name
