"""Уровни прокси и закрепление (M8.09): отчёты после `release`, два контекста на прокси, sticky без хранилища."""

from __future__ import annotations

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig, ProxyPolicy, StatePolicy
from browser_pool.config import Limits, Topology
from browser_pool.proxies import (
    Proxy,
    ProxyLease,
    ProxyList,
    ProxyOutcome,
    ProxyRequest,
    TieredProxyList,
)
from browser_pool.testing import FakeDriver

DC = [Proxy(host=f"10.0.0.{i}", port=80, id=f"dc{i}") for i in (1, 2)]
RESI = [Proxy(host="20.0.0.1", port=80, id="resi1")]


def request(key: str = "a", *, service: str = "mail", sticky: bool = False) -> ProxyRequest:
    return ProxyRequest(identity_key=key, labels={"service": service}, sticky=sticky)


async def take(source: TieredProxyList | ProxyList, req: ProxyRequest) -> ProxyLease:
    lease = await source.acquire(req)
    assert lease is not None
    return lease


# --- уровни ----------------------------------------------------------------------------


async def test_open_failures_reported_after_release_raise_the_tier() -> None:
    """Ядро зовёт `release`, потом `report(failed)` — сбои открытия всё равно считаются."""
    source = TieredProxyList(
        [DC, RESI], window=4, raise_at=0.5, breaker_failures=100, strategy="round_robin"
    )

    for index in range(4):
        lease = await take(source, request(f"id{index}"))
        await source.release(lease)
        await source.report(lease, ProxyOutcome.failed("timeout"))

    assert source.tier("mail") == 1
    assert (await take(source, request("next"))).proxy_id == "resi1"


async def test_release_of_one_context_does_not_erase_the_other() -> None:
    source = TieredProxyList(
        [[DC[0]], RESI], window=2, raise_at=0.5, breaker_failures=100, lower_after=100
    )
    first = await take(source, request("a"))
    second = await take(source, request("a"))  # второй контекст той же identity на том же прокси

    await source.release(first)
    await source.report(second, ProxyOutcome.failed("timeout"))
    await source.report(second, ProxyOutcome.failed("timeout"))

    assert source.tier("mail") == 1


# --- sticky ----------------------------------------------------------------------------


async def test_sticky_request_is_deterministic_for_any_strategy() -> None:
    proxies = [Proxy(host=f"10.0.1.{i}", port=80, id=f"p{i}") for i in range(1, 6)]
    first = ProxyList(proxies, strategy="round_robin")
    second = ProxyList(proxies, strategy="round_robin")

    picks_first = [(await take(first, request(f"id{n}", sticky=True))).proxy_id for n in range(10)]
    picks_second = [
        (await take(second, request(f"id{n}", sticky=True))).proxy_id for n in range(10)
    ]

    assert picks_first == picks_second
    assert len(set(picks_first)) > 1  # не всё на один


async def test_sticky_request_skips_a_resting_proxy_and_returns_when_it_recovers() -> None:
    proxies = [Proxy(host=f"10.0.1.{i}", port=80, id=f"p{i}") for i in range(1, 4)]
    source = ProxyList(proxies, breaker_cooldown=100.0)
    home = (await take(source, request("ada", sticky=True))).proxy_id

    await source.ban(home)
    elsewhere = (await take(source, request("ada", sticky=True))).proxy_id

    assert elsewhere != home


async def test_non_sticky_request_still_follows_the_strategy() -> None:
    source = ProxyList(DC, strategy="round_robin")

    ids = [(await take(source, request("a"))).proxy_id for _ in range(3)]

    assert ids == ["dc1", "dc2", "dc1"]


@pytest.mark.parametrize("mode", ["none", "read_only", "read_write"])
async def test_sticky_policy_keeps_the_proxy_after_the_pool_is_recreated(
    mode: str, fake_driver: FakeDriver
) -> None:
    proxies = [Proxy(host=f"10.0.2.{i}", port=80, id=f"p{i}") for i in range(1, 6)]
    identities = [
        Identity(
            key=f"mail:{n}",
            proxy=ProxyPolicy.sticky(),
            state=StatePolicy(mode=mode),  # pyright: ignore[reportArgumentType]
        )
        for n in range(8)
    ]

    async def pick() -> dict[str, str]:
        pool = BrowserPool(
            fake_driver,
            config=PoolConfig(
                topology=Topology(browsers=2, pages_per_browser=8),
                limits=Limits(spawn_delay=0.0),
            ),
            proxy_source=ProxyList(proxies, strategy="round_robin"),
        )
        chosen: dict[str, str] = {}
        async with pool:
            for identity in identities:
                async with pool.page(identity) as lease:
                    assert lease.proxy is not None
                    chosen[identity.key] = lease.proxy.label
        return chosen

    first = await pick()
    second = await pick()

    assert first == second
    assert len(set(first.values())) > 1
