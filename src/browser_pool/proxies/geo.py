"""Гео по прокси: откуда сайт увидит identity (гео-согласованность).

Контекст через немецкий прокси с часовым поясом Москвы и локалью en-US — заметен сайту.
`ProxyChecker` узнаёт выходной адрес прокси, страну и часовой пояс, и пул подставляет в контекст
`timezone`, `locale`, `geolocation` — только те, что identity не задала сама.

`HttpGeoChecker` спрашивает сервис гео по IP через сам прокси (по умолчанию ip-api.com — без
ключа, только http и только для некоммерческого использования: для работы возьмите свой сервис с
таким же ответом через `url`), кэширует ответ на `ttl`, а неудачу — на короткий `failure_ttl`:
мёртвый прокси не платит таймаут на каждом открытии. Кэш ограничен `max_entries`. Прокси `socks*`
стандартная библиотека не умеет — для них `None` (контекст откроется без подстановки).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from browser_pool.clock import monotonic
from browser_pool.proxies.proxy import Proxy

_logger = logging.getLogger(__name__)

DEFAULT_GEO_URL = "http://ip-api.com/json/?fields=status,query,countryCode,timezone,lat,lon"

_LOCALES = {
    "AT": "de-AT",
    "AU": "en-AU",
    "BE": "nl-BE",
    "BR": "pt-BR",
    "BY": "ru-BY",
    "CA": "en-CA",
    "CH": "de-CH",
    "CN": "zh-CN",
    "CZ": "cs-CZ",
    "DE": "de-DE",
    "DK": "da-DK",
    "ES": "es-ES",
    "FI": "fi-FI",
    "FR": "fr-FR",
    "GB": "en-GB",
    "GR": "el-GR",
    "HU": "hu-HU",
    "IE": "en-IE",
    "IN": "en-IN",
    "IT": "it-IT",
    "JP": "ja-JP",
    "KR": "ko-KR",
    "KZ": "ru-KZ",
    "MX": "es-MX",
    "NL": "nl-NL",
    "NO": "nb-NO",
    "PL": "pl-PL",
    "PT": "pt-PT",
    "RO": "ro-RO",
    "RU": "ru-RU",
    "SE": "sv-SE",
    "TR": "tr-TR",
    "UA": "uk-UA",
    "US": "en-US",
}
"""Основная локаль страны — для `navigator.language` и `Accept-Language`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyGeo:
    """Где выход прокси."""

    ip: str
    country: str | None = None
    """ISO 3166-1 alpha-2: `DE`."""
    timezone: str | None = None
    """IANA-имя: `Europe/Berlin`."""
    latitude: float | None = None
    longitude: float | None = None

    @property
    def locale(self) -> str | None:
        """Основная локаль страны; неизвестная страна — `None`."""
        return _LOCALES.get(self.country or "")


@runtime_checkable
class ProxyChecker(Protocol):
    """Узнать, где выход прокси."""

    async def check(self, proxy: Proxy) -> ProxyGeo | None:
        """Гео выхода; не удалось — `None`."""
        ...


type Fetch = Callable[[str, Proxy, float], Awaitable[bytes]]
"""GET адреса через прокси с таймаутом → тело ответа. Подменяется в тестах."""


class HttpGeoChecker:
    """Гео выхода прокси через сервис гео по IP. Реализует `ProxyChecker`."""

    def __init__(
        self,
        *,
        url: str = DEFAULT_GEO_URL,
        ttl: float = 3600.0,
        failure_ttl: float = 60.0,
        max_entries: int = 1024,
        timeout: float = 10.0,
        fetch: Fetch | None = None,
    ) -> None:
        """`url` — сервис с ответом как у ip-api.com; `ttl` — сколько помнить ответ, секунды.

        `failure_ttl` — сколько помнить неудачу (сбой запроса, `status=fail`, непонятный ответ);
        `max_entries` — сколько прокси держать в кэше, старые вытесняются.
        """
        if max_entries < 1:
            msg = f"max_entries должен быть ≥ 1, получено {max_entries}"
            raise ValueError(msg)
        self._url = url
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._max_entries = max_entries
        self._timeout = timeout
        self._fetch: Fetch = fetch if fetch is not None else _urllib_fetch
        self._cache: OrderedDict[str, tuple[float, ProxyGeo | None]] = OrderedDict()

    async def check(self, proxy: Proxy) -> ProxyGeo | None:
        """Гео выхода из кэша или запросом через сам прокси."""
        if proxy.scheme not in {"http", "https"}:
            return None
        key = proxy.url
        cached = self._cache.get(key)
        now = monotonic()
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(key)
            return cached[1]
        try:
            geo = _parse(await self._fetch(self._url, proxy, self._timeout))
        except Exception as error:  # noqa: BLE001 — протокол обещает `None`, а не исключение
            # Только тип: в тексте исключения бывает адрес прокси с кредами.
            _logger.warning("Гео прокси %s не определилось (%s)", proxy.label, type(error).__name__)
            geo = None
        self._remember(key, geo, now)
        return geo

    def _remember(self, key: str, geo: ProxyGeo | None, now: float) -> None:
        self._cache[key] = (now + (self._ttl if geo is not None else self._failure_ttl), geo)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)


def _parse(raw: bytes) -> ProxyGeo | None:
    answer: object = json.loads(raw)
    if not isinstance(answer, dict):
        return None
    answer = cast("dict[str, Any]", answer)
    if answer.get("status", "success") != "success" or not answer.get("query"):
        return None
    lat, lon = answer.get("lat"), answer.get("lon")
    return ProxyGeo(
        ip=str(answer["query"]),
        country=answer.get("countryCode"),
        timezone=answer.get("timezone"),
        latitude=float(lat) if isinstance(lat, int | float) else None,
        longitude=float(lon) if isinstance(lon, int | float) else None,
    )


async def _urllib_fetch(url: str, proxy: Proxy, seconds: float) -> bytes:
    return await asyncio.to_thread(_get, url, proxy.url, seconds)


def _get(url: str, proxy_url: str, seconds: float) -> bytes:
    """GET через http-прокси; креды из адреса прокси urllib отправит сам."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    with opener.open(url, timeout=seconds) as response:
        return cast("bytes", response.read())


__all__ = ["DEFAULT_GEO_URL", "Fetch", "HttpGeoChecker", "ProxyChecker", "ProxyGeo"]
