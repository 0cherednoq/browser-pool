"""Гео по прокси и уровни прокси: `ProxyChecker`, `TieredProxyList`."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

import pytest

from browser_pool import BrowserPool, ContextOptions, Identity, PoolConfig
from browser_pool.config import Limits, Topology
from browser_pool.geometry import Geolocation
from browser_pool.proxies import (
    HttpGeoChecker,
    Proxy,
    ProxyGeo,
    ProxyList,
    ProxyOutcome,
    ProxyRequest,
    TieredProxyList,
)
from browser_pool.testing import FakeDriver
from browser_pool.testing.contract_site import ContractProxy

DC = Proxy(host="10.0.0.1", port=8080, id="dc")
DC2 = Proxy(host="10.0.0.2", port=8080, id="dc2")
RESI = Proxy(host="10.0.1.1", port=8080, id="resi")
BERLIN = ProxyGeo(
    ip="5.6.7.8", country="DE", timezone="Europe/Berlin", latitude=52.5, longitude=13.4
)


def request(key: str, service: str = "mail") -> ProxyRequest:
    return ProxyRequest(identity_key=key, labels={"service": service})


# --- уровни -----------------------------------------------------------------------------


async def outcome(source: TieredProxyList, key: str, *, ok: bool, service: str = "mail") -> str:
    lease = await source.acquire(request(key, service))
    assert lease is not None
    await source.report(lease, ProxyOutcome.ok() if ok else ProxyOutcome.failed("timeout"))
    await source.release(lease)
    return lease.proxy_id


async def test_failures_raise_the_group_to_the_next_tier() -> None:
    source = TieredProxyList([[None], [DC, DC2], [RESI]], window=4, raise_at=0.5)

    assert await outcome(source, "a", ok=True) == "direct"
    for key in ("b", "c", "d"):
        await outcome(source, key, ok=False)

    assert source.tier("mail") == 1
    assert await outcome(source, "e", ok=True) in {"dc", "dc2"}


async def test_groups_have_their_own_tiers() -> None:
    source = TieredProxyList([[None], [DC]], window=2, raise_at=0.5)

    for key in ("a", "b"):
        await outcome(source, key, ok=False, service="shop")

    assert source.tier("shop") == 1
    assert source.tier("mail") == 0
    assert await outcome(source, "c", ok=True, service="mail") == "direct"


async def test_a_streak_of_successes_lowers_the_tier() -> None:
    source = TieredProxyList([[None], [DC]], window=2, raise_at=0.5, lower_after=3)
    for key in ("a", "b"):
        await outcome(source, key, ok=False)
    assert source.tier("mail") == 1

    for key in ("c", "d", "e"):
        await outcome(source, key, ok=True)

    assert source.tier("mail") == 0


async def test_exhausted_tier_falls_back_to_the_next_one() -> None:
    source = TieredProxyList([[DC], [RESI]], max_identities_per_proxy=1)

    first = await source.acquire(request("a"))
    second = await source.acquire(request("b"))

    assert first is not None
    assert second is not None
    assert (first.proxy_id, second.proxy_id) == ("dc", "resi")


def test_tiers_must_not_share_proxies() -> None:
    with pytest.raises(ValueError, match="уровнями"):
        TieredProxyList([[DC], [DC]])


# --- гео ------------------------------------------------------------------------------------


class Answers:
    """Фейковый сервис гео: ответ и счёт запросов."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def __call__(self, url: str, proxy: Proxy, seconds: float) -> bytes:
        _ = url, seconds
        self.calls.append(proxy.label)
        return json.dumps(self.answer).encode()


IP_API = {
    "status": "success",
    "query": "5.6.7.8",
    "countryCode": "DE",
    "timezone": "Europe/Berlin",
    "lat": 52.5,
    "lon": 13.4,
}


async def test_geo_is_parsed_and_cached() -> None:
    answers = Answers(IP_API)
    checker = HttpGeoChecker(fetch=answers, ttl=60)

    first = await checker.check(DC)
    second = await checker.check(DC)

    assert first == BERLIN
    assert first is not None
    assert first.locale == "de-DE"
    assert second == first
    assert answers.calls == ["dc"]  # второй раз — из кэша


async def test_failed_lookup_and_socks_give_no_geo() -> None:
    checker = HttpGeoChecker(fetch=Answers({"status": "fail", "message": "private range"}))

    assert await checker.check(DC) is None
    assert await checker.check(Proxy(scheme="socks5", host="10.0.0.9", port=1080)) is None


class GeoServer:
    """Настоящий HTTP-сервис гео на 127.0.0.1 — за него встанет тестовый прокси."""

    def __init__(self) -> None:
        body = json.dumps(IP_API).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            @override
            def log_message(self, format: str, *args: Any) -> None:
                _ = format, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def __enter__(self) -> GeoServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def geo_proxy() -> Iterator[tuple[Proxy, ContractProxy]]:
    with GeoServer() as server, ContractProxy(server, username="ada", password="pa55") as proxy:
        yield Proxy(host="127.0.0.1", port=proxy.port, username="ada", password="pa55"), proxy


async def test_default_fetch_asks_through_the_proxy(
    geo_proxy: tuple[Proxy, ContractProxy],
) -> None:
    proxy, gateway = geo_proxy
    checker = HttpGeoChecker(url="http://geo.invalid/json/")

    assert await checker.check(proxy) == BERLIN
    assert gateway.hosts == ["geo.invalid"]  # запрос ушёл через прокси, с кредами


# --- гео в контексте ---------------------------------------------------------------------------


class StaticChecker:
    def __init__(self, geo: ProxyGeo | None = BERLIN, *, error: Exception | None = None) -> None:
        self.geo = geo
        self.error = error

    async def check(self, proxy: Proxy) -> ProxyGeo | None:
        _ = proxy
        if self.error is not None:
            raise self.error
        return self.geo


def make_pool(driver: FakeDriver, checker: StaticChecker) -> BrowserPool[Any, Any, Any]:
    return BrowserPool(
        driver,
        config=PoolConfig(topology=Topology(browsers=1), limits=Limits(spawn_delay=0.0)),
        proxy_source=ProxyList([DC]),
        proxy_checker=checker,
    )


async def test_context_follows_the_proxy_exit(fake_driver: FakeDriver) -> None:
    async with (
        make_pool(fake_driver, StaticChecker()) as pool,
        pool.page(Identity(key="a")) as lease,
    ):
        spec = lease.context.spec
        assert (spec.timezone, spec.locale) == ("Europe/Berlin", "de-DE")
        assert spec.geolocation == Geolocation(latitude=52.5, longitude=13.4)


async def test_identity_settings_win_over_the_proxy_exit(fake_driver: FakeDriver) -> None:
    identity = Identity(key="a", context_options=ContextOptions(timezone="Asia/Tokyo"))

    async with make_pool(fake_driver, StaticChecker()) as pool, pool.page(identity) as lease:
        assert lease.context.spec.timezone == "Asia/Tokyo"
        assert lease.context.spec.locale == "de-DE"


async def test_failing_checker_does_not_block_the_context(fake_driver: FakeDriver) -> None:
    checker = StaticChecker(error=OSError("сервис гео недоступен"))

    async with make_pool(fake_driver, checker) as pool, pool.page(Identity(key="a")) as lease:
        assert lease.context.spec.timezone is None
