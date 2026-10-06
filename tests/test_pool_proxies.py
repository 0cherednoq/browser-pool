"""Прокси в пуле: один прокси на контекст, отчёты источнику, повтор с другим, sticky, лимиты."""

from __future__ import annotations

import asyncio
from typing import override

import pytest

from browser_pool import (
    BrowserPool,
    ErrorKind,
    Identity,
    PoolConfig,
    PoolSignal,
    ProxyPolicy,
    clock,
)
from browser_pool.config import Limits, Recovery, Recycling, Timeouts, Topology
from browser_pool.driver import DriverCapabilities
from browser_pool.errors import NoUsableProxyError, ProxyFailedError, UnsupportedRequirementError
from browser_pool.events import ContextRetired, NoUsableProxy, PoolEvent, ProxyFailed
from browser_pool.flow import BaseFlow, OpenRequest
from browser_pool.proxies import (
    CallbackProxySource,
    Proxy,
    ProxyLease,
    ProxyList,
    ProxyOutcome,
    ProxyRequest,
    ProxySource,
)
from browser_pool.state import Cookie, MemoryStateStore, StateStore
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

A = Identity(key="mail:a")
B = Identity(key="mail:b")
P1, P2, P3 = (Proxy(host=f"10.0.0.{index}", port=8080, id=f"p{index}") for index in (1, 2, 3))

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


class BadProxies(BaseFlow[FakeContext, FakePage, None]):
    """Вход не проходит через перечисленные прокси — сбой вида `proxy`."""

    def __init__(self, *bad: str, login_takes: float = 0.0) -> None:
        self.bad = set(bad)
        self.login_takes = login_takes
        self.seen: list[str | None] = []

    @override
    async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> None:
        proxy = ctx.proxy
        self.seen.append(proxy.label if proxy is not None else None)
        await asyncio.sleep(self.login_takes)
        if proxy is not None and proxy.label in self.bad:
            reason = "туннель не открылся"
            raise PoolSignal(reason, kind=ErrorKind.proxy)


class Recording:
    """Источник-обёртка: пишет, что у него просили и что ему сообщали."""

    def __init__(self, inner: ProxySource) -> None:
        self.inner = inner
        self.requests: list[ProxyRequest] = []
        self.reports: list[tuple[str, str]] = []
        self.released: list[str] = []

    async def acquire(self, request: ProxyRequest) -> ProxyLease | None:
        self.requests.append(request)
        return await self.inner.acquire(request)

    async def report(self, lease: ProxyLease, outcome: ProxyOutcome) -> None:
        self.reports.append((lease.proxy_id, outcome.kind))
        await self.inner.report(lease, outcome)

    async def release(self, lease: ProxyLease) -> None:
        self.released.append(lease.proxy_id)
        await self.inner.release(lease)


def make_pool(
    driver: FakeDriver,
    source: ProxySource | None,
    *,
    flow: BaseFlow[FakeContext, FakePage, None] | None = None,
    store: StateStore | None = None,
    limits: Limits | None = None,
    proxy_retries: int = 2,
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4),
            limits=limits or Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            recovery=Recovery(proxy_retries=proxy_retries),
            timeouts=Timeouts(open=60.0),
        ),
        flow=flow,
        state_store=store,
        proxy_source=source,
    )


def collect(pool: Pool) -> list[PoolEvent]:
    events: list[PoolEvent] = []
    pool.on(PoolEvent, events.append)
    return events


# --- выдача ----------------------------------------------------------------------------


async def test_context_gets_a_proxy_from_the_source(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, ProxyList([P1, P2])) as pool, pool.page(A) as lease:
        assert lease.proxy == P1
        assert lease.context.spec.proxy == P1


async def test_without_a_source_contexts_go_direct(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, None) as pool, pool.page(A) as lease:
        assert lease.proxy is None
        assert lease.context.spec.proxy is None


async def test_proxy_is_chosen_once_per_context(fake_driver: FakeDriver) -> None:
    source = Recording(ProxyList([P1, P2]))

    async with make_pool(fake_driver, source) as pool:
        for _ in range(3):
            async with pool.page(A) as lease:
                assert lease.proxy == P1

    assert len(source.requests) == 1
    assert source.released == ["p1"]  # отпущен, когда контекст закрылся


async def test_direct_and_fixed_policies_do_not_ask_the_source(fake_driver: FakeDriver) -> None:
    source = Recording(ProxyList([P1]))
    direct = Identity(key="direct", proxy=ProxyPolicy.direct())
    fixed = Identity(key="fixed", proxy=ProxyPolicy.fixed(P3))

    async with make_pool(fake_driver, source) as pool:
        async with pool.page(direct) as lease:
            assert lease.proxy is None
        async with pool.page(fixed) as lease:
            assert lease.proxy == P3

    assert source.requests == []


async def test_external_policy_leaves_the_proxy_to_the_vendor(fake_driver: FakeDriver) -> None:
    vendor = Identity(key="vendor", proxy=ProxyPolicy.external())

    async with make_pool(fake_driver, ProxyList([P1])) as pool, pool.page(vendor) as lease:
        assert lease.proxy is None


# --- сбой прокси -----------------------------------------------------------------------


async def test_failed_proxy_is_reported_and_replaced(fake_driver: FakeDriver) -> None:
    source = Recording(ProxyList([P1, P2], strategy="round_robin"))
    flow = BadProxies("p1")

    async with make_pool(fake_driver, source, flow=flow) as pool:
        events = collect(pool)
        async with pool.page(A) as lease:
            assert lease.proxy == P2

    assert flow.seen == ["p1", "p2"]
    assert ("p1", "failed") in source.reports
    assert ("p2", "ok") in source.reports
    assert source.requests[1].exclude == frozenset({"p1"})
    failed = [event for event in events if isinstance(event, ProxyFailed)]
    assert [(event.proxy, event.retrying) for event in failed] == [("p1", True)]
    assert "туннель" not in failed[0].reason  # только тип, без текста исключения


async def test_retries_are_bounded_and_the_error_names_the_proxy(fake_driver: FakeDriver) -> None:
    flow = BadProxies("p1", "p2", "p3")

    async with make_pool(fake_driver, ProxyList([P1, P2, P3]), flow=flow, proxy_retries=1) as pool:
        with pytest.raises(ProxyFailedError) as caught:
            async with pool.page(A):
                pass

    assert flow.seen == ["p1", "p2"]
    assert caught.value.proxy == P2
    assert caught.value.identity == "mail:a"


async def test_fixed_proxy_is_not_swapped(fake_driver: FakeDriver) -> None:
    flow = BadProxies("p3")
    fixed = Identity(key="fixed", proxy=ProxyPolicy.fixed(P3))

    async with make_pool(fake_driver, ProxyList([P1]), flow=flow) as pool:
        with pytest.raises(ProxyFailedError) as caught:
            async with pool.page(fixed):
                pass

    assert caught.value.proxy == P3
    assert flow.seen == ["p3"]


async def test_no_usable_proxy_is_an_error_and_an_event(fake_driver: FakeDriver) -> None:
    source = CallbackProxySource(lambda _request: None)

    async with make_pool(fake_driver, source) as pool:
        events = collect(pool)
        with pytest.raises(NoUsableProxyError) as caught:
            async with pool.page(A):
                pass

    assert caught.value.identity == "mail:a"
    assert any(isinstance(event, NoUsableProxy) for event in events)
    assert fake_driver.live.contexts == 0


async def test_proxy_failing_during_a_lease_is_reported_and_the_context_retired(
    fake_driver: FakeDriver,
) -> None:
    source = Recording(ProxyList([P1, P2], strategy="round_robin"))

    async with make_pool(fake_driver, source) as pool:
        events = collect(pool)
        async with pool.page(A) as lease:
            lease.report(ErrorKind.proxy)
        await asyncio.sleep(0)
        async with pool.page(A) as lease:
            assert lease.proxy == P2  # новый контекст — новый прокси

    assert ("p1", "failed") in source.reports
    assert any(isinstance(event, ContextRetired) for event in events)
    assert any(isinstance(event, ProxyFailed) and not event.retrying for event in events)


# --- sticky ----------------------------------------------------------------------------


async def test_sticky_identity_gets_its_proxy_back_after_restart(fake_driver: FakeDriver) -> None:
    store = MemoryStateStore()
    sticky = Identity(key="mail:sticky", proxy=ProxyPolicy.sticky())

    async with make_pool(fake_driver, ProxyList([P1, P2, P3]), store=store) as pool:
        async with pool.page(B) as lease:
            assert lease.proxy == P1  # обычная identity — по кругу
        async with pool.page(sticky) as lease:
            home = lease.proxy
    assert home is not None

    record = await store.load("mail:sticky")
    assert record is not None
    assert record.proxy_ref == home.label

    # Новый процесс: список в другом порядке, по кругу первым отдал бы p3 — а identity закреплена.
    async with (
        make_pool(fake_driver, ProxyList([P3, P2, P1]), store=store) as pool,
        pool.page(sticky) as lease,
    ):
        assert lease.proxy == home


# --- возможности драйвера и лимиты -----------------------------------------------------


async def test_proxies_the_driver_cannot_use_are_skipped() -> None:
    driver = FakeDriver(
        capabilities=DriverCapabilities(
            proxy_scope="context",
            can_new_context=True,
            state_support="full",
            proxy_schemes=frozenset({"http"}),
        )
    )
    socks = Proxy(scheme="socks5", host="10.0.0.9", port=1080, id="socks")
    authed = Proxy(host="10.0.0.8", port=8080, username="ada", password="pa55", id="authed")

    async with make_pool(driver, ProxyList([socks, authed, P1])) as pool:
        async with pool.page(A) as lease:
            assert lease.proxy == P1
        with pytest.raises(UnsupportedRequirementError, match="socks5"):
            async with pool.page(Identity(key="fixed", proxy=ProxyPolicy.fixed(socks))):
                pass


async def test_identities_per_proxy_are_capped_for_any_source(fake_driver: FakeDriver) -> None:
    def pick(request: ProxyRequest) -> Proxy | None:
        return next((p for p in (P1, P2) if p.label not in request.exclude), None)

    limits = Limits(spawn_delay=0.0, max_identities_per_proxy=1)

    async with (
        make_pool(fake_driver, CallbackProxySource(pick), limits=limits) as pool,
        pool.page(A) as first,
        pool.page(B) as second,
    ):
        assert (first.proxy, second.proxy) == (P1, P2)


async def test_opens_through_one_proxy_are_serialised(fake_driver: FakeDriver) -> None:
    flow = BadProxies(login_takes=10.0)
    source = CallbackProxySource(lambda _request: P1)
    done: dict[str, float] = {}

    async with make_pool(fake_driver, source, flow=flow) as pool:
        started = clock.monotonic()

        async def work(identity: Identity) -> None:
            async with pool.page(identity):
                done[identity.key] = clock.monotonic() - started

        await asyncio.gather(work(A), work(B))

    assert sorted(done.values()) == pytest.approx([10.0, 20.0])


async def test_opens_through_different_proxies_run_together(fake_driver: FakeDriver) -> None:
    flow = BadProxies(login_takes=10.0)
    done: dict[str, float] = {}

    async with make_pool(fake_driver, ProxyList([P1, P2]), flow=flow) as pool:
        started = clock.monotonic()

        async def work(identity: Identity) -> None:
            async with pool.page(identity):
                done[identity.key] = clock.monotonic() - started

        await asyncio.gather(work(A), work(B))

    assert sorted(done.values()) == pytest.approx([10.0, 10.0])


# --- handoff в HTTP-клиент -------------------------------------------------------------


async def test_lease_hands_cookies_to_an_http_client(fake_driver: FakeDriver) -> None:
    mail = Cookie(name="sid", value="1", domain=".mail.example")
    other = Cookie(name="ads", value="2", domain="ads.example")

    async with make_pool(fake_driver, ProxyList([P1])) as pool, pool.page(A) as lease:
        await fake_driver.add_cookies(lease.context, [mail, other])

        assert set(await lease.cookies()) == {mail, other}
        assert await lease.cookies(domain="inbox.mail.example") == (mail,)
        assert lease.proxy is not None
        assert lease.proxy.url == "http://10.0.0.1:8080"
