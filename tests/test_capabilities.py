"""Планирование по capabilities: владелец браузера, эффективный конфиг, невыполнимое.

Сценарии M1 — выдача, ожидание, смена владельца, прокси, падение, вывод из работы, аренда
контекста, варианты — прогоняются на фейке с каждой комбинацией возможностей драйвера.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Hashable
from pathlib import Path

import pytest
import pytest_asyncio

from browser_pool import BrowserPool, Identity, PoolConfig, ProxyPolicy
from browser_pool._core.capabilities import (
    browser_owner,
    effective_config,
    shares_browser,
    unmet,
)
from browser_pool.config import Lifecycle, Limits, Recycling, Timeouts, Topology
from browser_pool.driver import DriverCapabilities
from browser_pool.errors import UnsupportedRequirementError
from browser_pool.identity import StatePolicy
from browser_pool.proxies import SCHEMES, Proxy, ProxyList
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage
from browser_pool.testing.fake_driver import FAKE_CAPABILITIES

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

A = Identity(key="mail:a")
B = Identity(key="mail:b")
P1 = Proxy(host="10.0.0.1", port=8080, id="p1")
P2 = Proxy(host="10.0.0.2", port=8080, id="p2")

SHARED = DriverCapabilities(
    proxy_scope="browser",
    can_new_context=True,
    fingerprint_scope="context",
    state_support="full",
    proxy_auth=True,
    proxy_schemes=SCHEMES,
)
"""Прокси на браузере, но контексты и отпечаток на контексте: браузер делят по прокси."""
FINGERPRINTED = DriverCapabilities(
    proxy_scope="browser",
    can_new_context=True,
    fingerprint_scope="browser",
    state_support="full",
    proxy_auth=True,
    proxy_schemes=SCHEMES,
)
"""Как Camoufox: прокси и отпечаток при запуске — браузер одной identity."""
SINGLE = DriverCapabilities(
    proxy_scope="browser",
    can_new_context=False,
    state_support="cookies",
    proxy_schemes=frozenset({"http", "https"}),
    thread_affinity=True,
)
"""Как Selenium: один контекст на процесс, прокси при запуске, без авторизации на прокси, синхронный."""
EXTERNAL = DriverCapabilities(
    proxy_scope="external",
    can_new_context=False,
    fingerprint_scope="external",
    state_support="cookies",
)
"""Антидетект: браузер — профиль вендора, прокси и отпечаток его."""

COMBINATIONS = {
    "context": FAKE_CAPABILITIES,
    "browser-shared": SHARED,
    "browser-fingerprint": FINGERPRINTED,
    "browser-single": SINGLE,
    "external": EXTERNAL,
}
PER_IDENTITY = {"browser-fingerprint", "browser-single", "external"}
"""Комбинации, где браузер принадлежит одной identity всегда."""


@pytest_asyncio.fixture(params=list(COMBINATIONS), ids=list(COMBINATIONS))
async def driver(request: pytest.FixtureRequest) -> AsyncIterator[FakeDriver]:
    """Фейк с одной из комбинаций возможностей; после теста — ничего не утекло."""
    fake = FakeDriver(capabilities=COMBINATIONS[request.param])
    yield fake
    await asyncio.sleep(0)
    assert tuple(fake.live) == (0, 0, 0), f"утечка: {fake.live}"


def combination(driver: FakeDriver) -> str:
    return next(name for name, caps in COMBINATIONS.items() if caps is driver.capabilities)


def make_pool(
    driver: FakeDriver, *, browsers: int = 1, proxies: ProxyList | None = None, **topology: int
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=browsers, pages_per_browser=4, **topology),
            limits=Limits(spawn_delay=0.0),
            lifecycle=Lifecycle(healthcheck_interval=10.0),
            recycling=Recycling(recycle_jitter=0.0),
            timeouts=Timeouts(close=2.0, restart=5.0, startup=5.0),
        ),
        proxy_source=proxies,
    )


def launches(driver: FakeDriver) -> int:
    return sum(call.operation == "launch" for call in driver.calls)


async def hold(pool: Pool, identity: Identity, release: asyncio.Event) -> FakeBrowser:
    async with pool.page(identity) as lease:
        await release.wait()
        return lease.browser


# --- владелец и эффективный конфиг -----------------------------------------------------


def test_only_browser_scoped_drivers_with_contexts_share_browsers() -> None:
    assert [name for name, caps in COMBINATIONS.items() if shares_browser(caps)] == [
        "browser-shared"
    ]
    no_profiles = dataclasses.replace(FAKE_CAPABILITIES, persistent_dir=False)
    assert browser_owner(no_profiles, proxy_source=True) is None
    # С профилями на диске владелец есть только у identity с профилем; остальные — в общих.
    owner = browser_owner(FAKE_CAPABILITIES, proxy_source=True)
    assert owner is not None
    assert owner(A) is None
    profiled = Identity(key="p", state=StatePolicy(user_data_dir=Path("profiles/p")))
    assert owner(profiled) == ("profile", "p", Path("profiles/p"))


@pytest.mark.parametrize(
    ("identity", "proxy_source", "owner"),
    [
        (Identity(key="a", proxy=ProxyPolicy.fixed(P1)), True, ("proxy", P1)),
        (Identity(key="a", proxy=ProxyPolicy.direct()), True, ("direct",)),
        (Identity(key="a"), False, ("direct",)),
        (Identity(key="a"), True, ("identity", "a")),
        (Identity(key="a", proxy=ProxyPolicy.sticky()), True, ("identity", "a")),
        (Identity(key="a", proxy=ProxyPolicy.external()), False, ("identity", "a")),
    ],
    ids=["fixed", "direct", "pool-without-source", "pool", "sticky", "external"],
)
def test_shared_browser_owner_is_the_proxy_known_in_advance(
    identity: Identity, proxy_source: bool, owner: Hashable
) -> None:
    decide = browser_owner(SHARED, proxy_source=proxy_source)
    assert decide is not None
    assert decide(identity) == owner


@pytest.mark.parametrize("name", sorted(PER_IDENTITY))
def test_browser_of_one_identity_has_one_context(name: str) -> None:
    config = PoolConfig(topology=Topology(contexts_per_browser=12))
    capabilities = COMBINATIONS[name]
    decide = browser_owner(capabilities, proxy_source=False)

    assert effective_config(config, capabilities).topology.contexts_per_browser == 1
    assert decide is not None
    assert decide(Identity(key="a", proxy=ProxyPolicy.direct())) == ("identity", "a")


@pytest.mark.parametrize("name", ["context", "browser-shared"])
def test_drivers_that_share_browsers_keep_the_config(name: str) -> None:
    config = PoolConfig(topology=Topology(contexts_per_browser=12))
    assert effective_config(config, COMBINATIONS[name]) is config


def test_page_hint_of_the_driver_lowers_pages_per_browser() -> None:
    capabilities = DriverCapabilities(proxy_scope="context", can_new_context=True, max_pages_hint=2)
    config = PoolConfig(topology=Topology(pages_per_browser=8, warm_pages_per_identity=3))

    topology = effective_config(config, capabilities).topology

    assert (topology.pages_per_browser, topology.warm_pages_per_identity) == (2, 2)


async def test_pool_shows_both_configs(driver: FakeDriver) -> None:
    async with make_pool(driver, contexts_per_browser=6) as pool:
        assert pool.config.topology.contexts_per_browser == 6
        expected = 1 if combination(driver) in PER_IDENTITY else 6
        assert pool.effective_config.topology.contexts_per_browser == expected


# --- невыполнимое ----------------------------------------------------------------------

SOCKS = Proxy(scheme="socks5", host="10.0.0.9", port=1080, username="ada", password="pa55")


def test_unmet_lists_every_mismatch_at_once() -> None:
    identity = Identity(
        key="a",
        proxy=ProxyPolicy.fixed(SOCKS),
        state=StatePolicy(user_data_dir=Path("profile")),
    )

    problems = unmet(identity, SINGLE)

    assert len(problems) == 3
    assert any("user_data_dir" in problem for problem in problems)
    assert any("socks5" in problem for problem in problems)
    assert any("авторизация" in problem for problem in problems)
    assert unmet(identity, EXTERNAL) == (problems[0],)  # прокси вендора — не забота пула


async def test_unsupported_requirement_fails_at_acquire_not_in_the_queue() -> None:
    driver = FakeDriver(capabilities=SINGLE)
    impossible = Identity(key="socks", proxy=ProxyPolicy.fixed(SOCKS))

    async with make_pool(driver) as pool:
        with pytest.raises(UnsupportedRequirementError) as raised:
            async with pool.page(impossible):
                pass
        async with pool.page(any_of=[impossible, A]) as lease:
            assert lease.identity is A

    assert len(raised.value.missing) == 2
    assert launches(driver) == 1  # невыполнимая заявка не запускала браузер


async def test_context_lease_checks_requirements_too() -> None:
    driver = FakeDriver(capabilities=SINGLE)
    async with make_pool(driver) as pool:
        with pytest.raises(UnsupportedRequirementError, match="socks5"):
            async with pool.context(Identity(key="socks", proxy=ProxyPolicy.fixed(SOCKS))):
                pass


# --- сценарии M1 на каждой комбинации --------------------------------------------------


async def test_lease_returns_the_page_warm(driver: FakeDriver) -> None:
    async with make_pool(driver) as pool:
        async with pool.page(A) as lease:
            first = lease.page
        async with pool.page(A) as lease:
            assert lease.page is first
            assert lease.generation == 1
    assert launches(driver) == 1


async def test_identities_work_side_by_side_in_their_browsers(driver: FakeDriver) -> None:
    release = asyncio.Event()
    async with make_pool(driver, browsers=2) as pool:
        tasks = [asyncio.create_task(hold(pool, identity, release)) for identity in (A, B)]
        await asyncio.sleep(0.1)
        assert pool.snapshot().leases_active == 2
        release.set()
        first, second = await asyncio.gather(*tasks)

    assert first is not second  # «самый свободный» браузер — у каждой свой
    assert launches(driver) == 2


async def test_new_owner_waits_for_the_browser_and_gets_it_relaunched(driver: FakeDriver) -> None:
    release = asyncio.Event()
    async with make_pool(driver, proxies=ProxyList([P1, P2])) as pool:
        holder = asyncio.create_task(hold(pool, A, release))
        await asyncio.sleep(0.1)
        waiter = asyncio.create_task(hold(pool, B, asyncio.Event()))
        await asyncio.sleep(0.1)
        shared_browser = combination(driver) == "context"
        assert pool.snapshot().leases_active == (2 if shared_browser else 1)

        release.set()
        first = await holder
        if not shared_browser:
            await asyncio.sleep(0.1)
            assert pool.snapshot().leases_active == 1  # B получил браузер, освобождённый от A
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        contexts = pool.snapshot().contexts

    assert {context.key for context in contexts} == (
        {"mail:a", "mail:b"} if shared_browser else {"mail:b"}
    )
    if shared_browser:
        assert launches(driver) == 1
    else:
        assert launches(driver) == 2  # браузер перезапущен под нового владельца
        assert not first.alive


async def test_proxy_lands_where_the_driver_takes_it(driver: FakeDriver) -> None:
    async with make_pool(driver, proxies=ProxyList([P1])) as pool, pool.page(A) as lease:
        scope = driver.capabilities.proxy_scope
        launched = lease.browser.spec
        assert launched is not None
        if scope == "external":
            assert (lease.proxy, launched.proxy, lease.context.spec.proxy) == (None, None, None)
        elif scope == "browser":
            assert (lease.proxy, launched.proxy, lease.context.spec.proxy) == (P1, P1, None)
        else:
            assert (lease.proxy, launched.proxy, lease.context.spec.proxy) == (P1, None, P1)


async def test_crashed_browser_is_replaced(driver: FakeDriver) -> None:
    async with make_pool(driver) as pool:
        async with pool.page(A) as lease:
            crashed = lease.browser
        driver.crash(crashed)
        await asyncio.sleep(1.0)

        async with pool.page(A) as lease:
            assert lease.browser is not crashed
            assert lease.page.alive
            assert lease.generation == 2


async def test_retired_context_reopens(driver: FakeDriver) -> None:
    async with make_pool(driver) as pool:
        async with pool.page(A) as lease:
            await lease.retire_context("проверка")
            first = lease.context
        await asyncio.sleep(0.1)

        async with pool.page(A) as lease:
            assert lease.generation == 2
            assert lease.context is not first
            assert not first.alive  # закрыт — или ушёл вместе с браузером без can_new_context

    expected = 1 if driver.capabilities.can_new_context else 2
    assert launches(driver) == expected  # без can_new_context контекст — это новый процесс


async def test_whole_context_lease(driver: FakeDriver) -> None:
    async with make_pool(driver) as pool:
        async with pool.context(A) as lease:
            assert lease.context.alive
        async with pool.page(A) as lease:
            assert lease.generation == 1


async def test_variant_switch_with_another_proxy(driver: FakeDriver) -> None:
    via_proxy = Identity(key="mail:a", variant="proxy", proxy=ProxyPolicy.fixed(P1))
    direct = Identity(key="mail:a", variant="direct", proxy=ProxyPolicy.direct())
    external = driver.capabilities.proxy_scope == "external"

    async with make_pool(driver) as pool:
        async with pool.page(via_proxy) as lease:
            assert lease.proxy == (None if external else P1)
        async with pool.page(direct) as lease:
            assert lease.proxy is None
            assert lease.identity.variant == "direct"
            if driver.capabilities.proxy_scope == "browser":
                assert lease.browser.spec is not None
                assert lease.browser.spec.proxy is None


async def test_any_of_prefers_the_open_context(driver: FakeDriver) -> None:
    async with make_pool(driver, browsers=2) as pool:
        async with pool.page(B):
            pass
        async with pool.page(any_of=[A, B]) as lease:
            assert lease.identity is B


# --- общий браузер по прокси -----------------------------------------------------------


async def test_identities_with_the_same_proxy_share_a_browser() -> None:
    driver = FakeDriver(capabilities=SHARED)
    first = Identity(key="a", proxy=ProxyPolicy.fixed(P1))
    second = Identity(key="b", proxy=ProxyPolicy.fixed(P1))
    other = Identity(key="c", proxy=ProxyPolicy.fixed(P2))
    release = asyncio.Event()

    async with make_pool(driver) as pool:
        tasks = [asyncio.create_task(hold(pool, identity, release)) for identity in (first, second)]
        await asyncio.sleep(0.1)
        stranger = asyncio.create_task(hold(pool, other, release))
        await asyncio.sleep(0.1)
        assert pool.snapshot().leases_active == 2  # чужой прокси ждёт свой браузер
        release.set()
        browsers = await asyncio.gather(*tasks, stranger)

    assert browsers[0] is browsers[1]
    assert browsers[2] is not browsers[0]
    assert [browser.spec.proxy for browser in driver.browsers if browser.spec] == [P1, P2]


# --- авторизация на прокси по схемам -------------------------------------------------------

CHROMIUM = DriverCapabilities(
    proxy_scope="context",
    can_new_context=True,
    proxy_auth=True,
    proxy_schemes=frozenset({"http", "https", "socks5"}),
    proxy_auth_schemes=frozenset({"http", "https"}),
)
"""Как у драйверов поставки: socks понимает, но без логина — Chromium на socks не авторизуется."""


def test_socks_with_credentials_is_unmet_where_only_http_authenticates() -> None:
    with_login = Identity(key="socks:login", proxy=ProxyPolicy.fixed(SOCKS))
    open_socks = Identity(
        key="socks:open",
        proxy=ProxyPolicy.fixed(Proxy(scheme="socks5", host="10.0.0.9", port=1080)),
    )
    http = Identity(
        key="http:login",
        proxy=ProxyPolicy.fixed(Proxy(host="10.0.0.9", port=8080, username="ada", password="pa55")),
    )

    (problem,) = unmet(with_login, CHROMIUM)

    assert "socks5" in problem
    assert "http" in problem  # и что драйвер умеет взамен
    assert unmet(open_socks, CHROMIUM) == ()
    assert unmet(http, CHROMIUM) == ()


async def test_socks_with_credentials_is_refused_at_the_lease() -> None:
    driver = FakeDriver(capabilities=CHROMIUM)

    async with BrowserPool(driver) as pool:
        with pytest.raises(UnsupportedRequirementError, match="socks5"):
            async with pool.page(Identity(key="socks", proxy=ProxyPolicy.fixed(SOCKS))):
                pass

    # Отказ — до очереди и до браузера: драйвер даже не запускали.
    assert {call.operation for call in driver.calls} <= {"prepare", "shutdown"}
