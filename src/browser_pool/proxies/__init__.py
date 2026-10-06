"""Прокси: значение, разбор строк, источники, выбор и здоровье."""

from __future__ import annotations

from browser_pool.proxies.geo import HttpGeoChecker, ProxyChecker, ProxyGeo
from browser_pool.proxies.proxy import SCHEMES, Proxy, ProxyFormatError
from browser_pool.proxies.sources import (
    DIRECT,
    CallbackProxySource,
    OutcomeKind,
    ProxyLease,
    ProxyList,
    ProxyOutcome,
    ProxyRequest,
    ProxySource,
    ProxyStatus,
    ProxyStrategy,
    proxy_id,
)
from browser_pool.proxies.tiers import TieredProxyList

__all__ = [
    "DIRECT",
    "SCHEMES",
    "CallbackProxySource",
    "HttpGeoChecker",
    "OutcomeKind",
    "Proxy",
    "ProxyChecker",
    "ProxyFormatError",
    "ProxyGeo",
    "ProxyLease",
    "ProxyList",
    "ProxyOutcome",
    "ProxyRequest",
    "ProxySource",
    "ProxyStatus",
    "ProxyStrategy",
    "TieredProxyList",
    "proxy_id",
]
