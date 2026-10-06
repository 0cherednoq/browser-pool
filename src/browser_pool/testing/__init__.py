"""Тестовый набор: фейковый драйвер и виртуальное время — для тестов ядра и приложений.

Пакет не требует pytest. Плагин для pytest — отдельный модуль
`browser_pool.testing.pytest_plugin`; контрактный набор для своего драйвера —
`browser_pool.testing.contract.DriverContractSuite` (тоже требует pytest, поэтому не здесь).
"""

from __future__ import annotations

from browser_pool.testing.fake_driver import (
    DEFAULT_SCREEN,
    FAKE_CAPABILITIES,
    FakeBrowser,
    FakeBrowserCrashedError,
    FakeCall,
    FakeContext,
    FakeDriver,
    FakeDriverError,
    FakeFaults,
    FakeOperation,
    FakePage,
    FakeTargetClosedError,
    FakeWindow,
    LiveResources,
)
from browser_pool.testing.fake_host import FakeHostProbe
from browser_pool.testing.fake_http import FakeNetworkError, FakeProxyError
from browser_pool.testing.fake_provider import FakeEndpointProvider
from browser_pool.testing.virtual_time import VirtualTimeLoop

__all__ = [
    "DEFAULT_SCREEN",
    "FAKE_CAPABILITIES",
    "FakeBrowser",
    "FakeBrowserCrashedError",
    "FakeCall",
    "FakeContext",
    "FakeDriver",
    "FakeDriverError",
    "FakeEndpointProvider",
    "FakeFaults",
    "FakeHostProbe",
    "FakeNetworkError",
    "FakeOperation",
    "FakePage",
    "FakeProxyError",
    "FakeTargetClosedError",
    "FakeWindow",
    "LiveResources",
    "VirtualTimeLoop",
]
