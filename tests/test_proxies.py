"""Прокси: разбор строк, источники, стратегии, выключатель, лимит identity на прокси."""

from __future__ import annotations

import asyncio

import pytest

from browser_pool.proxies import (
    CallbackProxySource,
    Proxy,
    ProxyFormatError,
    ProxyLease,
    ProxyList,
    ProxyOutcome,
    ProxyRequest,
    ProxySource,
)


def request(key: str = "mail:a", **kwargs: object) -> ProxyRequest:
    return ProxyRequest(identity_key=key, **kwargs)  # pyright: ignore[reportArgumentType] — поля заявки


# --- разбор ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("1.2.3.4:8080", Proxy(host="1.2.3.4", port=8080)),
        (
            "1.2.3.4:8080:ada:pa55",
            Proxy(host="1.2.3.4", port=8080, username="ada", password="pa55"),
        ),
        (
            "ada:pa55@1.2.3.4:8080",
            Proxy(host="1.2.3.4", port=8080, username="ada", password="pa55"),
        ),
        (
            "socks5://ada:pa55@proxy.example:1080",
            Proxy(
                scheme="socks5", host="proxy.example", port=1080, username="ada", password="pa55"
            ),
        ),
        ("https://proxy.example:443", Proxy(scheme="https", host="proxy.example", port=443)),
        ("[::1]:3128", Proxy(host="::1", port=3128)),
        ("  1.2.3.4:8080  ", Proxy(host="1.2.3.4", port=8080)),
        (
            "http://ada:p%40ss@1.2.3.4:8080",
            Proxy(host="1.2.3.4", port=8080, username="ada", password="p@ss"),
        ),
    ],
)
def test_proxy_lines_are_understood(line: str, expected: Proxy) -> None:
    assert Proxy.parse(line) == expected


def test_parsed_proxy_keeps_its_source_id() -> None:
    assert Proxy.parse("1.2.3.4:8080", id="db-17").id == "db-17"


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("", "пуст"),
        ("1.2.3.4", "порт"),
        ("1.2.3.4:http", "порт"),
        ("ftp://1.2.3.4:21", "схема"),
        ("1.2.3.4:8080:onlyuser", "формат"),
    ],
)
def test_bad_proxy_lines_are_explained(line: str, fragment: str) -> None:
    with pytest.raises(ProxyFormatError, match=fragment):
        Proxy.parse(line)


def test_parse_error_never_shows_the_password() -> None:
    with pytest.raises(ProxyFormatError) as caught:
        Proxy.parse("ada:hunter2@1.2.3.4:notaport")

    assert "hunter2" not in str(caught.value)


def test_lines_are_deduplicated_and_comments_skipped() -> None:
    source = ProxyList.parse_lines(
        [
            "# рабочие",
            "1.2.3.4:8080",
            "http://1.2.3.4:8080",  # тот же прокси в другой записи
            "",
            "direct",
            "5.6.7.8:3128:ada:pa55",
        ]
    )

    assert source.labels() == (
        "http://1.2.3.4:8080",
        "direct",
        Proxy.parse("5.6.7.8:3128:ada:pa55").label,
    )


# --- стратегии -------------------------------------------------------------------------

POOL = [Proxy(host=f"10.0.0.{index}", port=8080, id=f"p{index}") for index in range(1, 5)]


async def take(source: ProxySource, req: ProxyRequest) -> ProxyLease:
    lease = await source.acquire(req)
    assert lease is not None
    return lease


async def test_round_robin_goes_around() -> None:
    source = ProxyList(POOL[:3], strategy="round_robin")

    ids = [(await take(source, request())).proxy_id for _ in range(4)]

    assert ids == ["p1", "p2", "p3", "p1"]


async def test_least_used_prefers_the_idlest() -> None:
    source = ProxyList(POOL[:2], strategy="least_used")
    first = await take(source, request("a"))

    second = await take(source, request("b"))
    await source.release(first)
    third = await take(source, request("c"))

    assert (first.proxy_id, second.proxy_id, third.proxy_id) == ("p1", "p2", "p1")


async def test_sticky_gives_an_identity_the_same_proxy() -> None:
    source = ProxyList(POOL, strategy="sticky")

    picks = {(await take(source, request("mail:ada"))).proxy_id for _ in range(5)}

    assert len(picks) == 1


async def test_sticky_survives_removing_another_proxy() -> None:
    keys = [f"mail:{index}" for index in range(200)]
    full = ProxyList(POOL, strategy="sticky")
    before = {key: (await take(full, request(key))).proxy_id for key in keys}
    reduced = ProxyList([proxy for proxy in POOL if proxy.id != "p2"], strategy="sticky")

    after = {key: (await take(reduced, request(key))).proxy_id for key in keys}

    moved = [key for key in keys if before[key] != after[key]]
    assert moved
    assert all(before[key] == "p2" for key in moved)  # переехали только жители удалённого


async def test_sticky_survives_adding_a_proxy() -> None:
    keys = [f"mail:{index}" for index in range(200)]
    small = ProxyList(POOL[:3], strategy="sticky")
    before = {key: (await take(small, request(key))).proxy_id for key in keys}
    grown = ProxyList(POOL, strategy="sticky")

    after = {key: (await take(grown, request(key))).proxy_id for key in keys}

    moved = [key for key in keys if before[key] != after[key]]
    assert moved
    assert all(after[key] == "p4" for key in moved)  # переехали только на новый


async def test_repeated_trips_rest_longer() -> None:
    source = ProxyList(POOL[:1], breaker_failures=1, breaker_cooldown=10.0)
    lease = await take(source, request())

    await source.report(lease, ProxyOutcome.failed("timeout"))
    await asyncio.sleep(11)
    await source.report(lease, ProxyOutcome.failed("timeout"))  # второе срабатывание подряд — 20 с
    await asyncio.sleep(11)

    assert await source.acquire(request("next")) is None
    await asyncio.sleep(10)
    assert await source.acquire(request("next")) is not None


async def test_preferred_proxy_is_honoured_while_it_is_usable() -> None:
    source = ProxyList(POOL, strategy="round_robin")

    lease = await take(source, request(preferred="p3"))

    assert lease.proxy_id == "p3"


async def test_excluded_proxies_are_skipped() -> None:
    source = ProxyList(POOL[:2], strategy="round_robin")

    lease = await take(source, request(exclude=frozenset({"p1"})))

    assert lease.proxy_id == "p2"
    assert await source.acquire(request(exclude=frozenset({"p1", "p2"}))) is None


async def test_direct_entry_gives_a_lease_without_proxy() -> None:
    source = ProxyList([None])

    lease = await take(source, request())

    assert lease.proxy is None
    assert lease.proxy_id == "direct"


# --- выключатель и лимиты --------------------------------------------------------------


async def test_failing_proxy_rests_and_comes_back() -> None:
    source = ProxyList(POOL[:2], strategy="least_used", breaker_failures=2, breaker_cooldown=60.0)
    bad = await take(source, request())
    for _ in range(2):
        await source.report(bad, ProxyOutcome.failed("timeout"))
    await source.release(bad)

    picks = {(await take(source, request(str(index)))).proxy_id for index in range(3)}
    assert picks == {"p2"}

    await asyncio.sleep(61)
    assert (await take(source, request("late"))).proxy_id == "p1"  # отдохнул и свободнее всех


async def test_success_resets_the_failure_count() -> None:
    source = ProxyList(POOL[:1], breaker_failures=2)
    lease = await take(source, request())

    await source.report(lease, ProxyOutcome.failed("timeout"))
    await source.report(lease, ProxyOutcome.ok())
    await source.report(lease, ProxyOutcome.failed("timeout"))

    assert await source.acquire(request("next")) is not None


async def test_banned_proxy_rests_at_once() -> None:
    source = ProxyList(POOL[:1], breaker_failures=5)
    lease = await take(source, request())

    await source.report(lease, ProxyOutcome.banned("403 от сайта"))

    assert await source.acquire(request("next")) is None


async def test_identities_per_proxy_are_capped() -> None:
    source = ProxyList(POOL[:2], strategy="round_robin", max_identities_per_proxy=1)
    first = await take(source, request("a"))
    second = await take(source, request("b"))

    assert await source.acquire(request("c")) is None
    await source.release(first)
    assert (await take(source, request("c"))).proxy_id == first.proxy_id
    assert second.proxy_id != first.proxy_id


def test_empty_list_is_refused() -> None:
    with pytest.raises(ValueError, match="пуст"):
        ProxyList([])


# --- источник приложения ---------------------------------------------------------------


async def test_callback_source_asks_the_application() -> None:
    reports: list[tuple[str, str]] = []
    released: list[str] = []

    async def pick(req: ProxyRequest) -> Proxy | None:
        return None if "p1" in req.exclude else POOL[0]

    source = CallbackProxySource(
        pick,
        on_report=lambda lease, outcome: reports.append((lease.proxy_id, outcome.kind)),
        on_release=lambda lease: released.append(lease.proxy_id),
    )

    lease = await take(source, request())
    await source.report(lease, ProxyOutcome.failed("timeout"))
    await source.release(lease)

    assert lease.proxy is POOL[0]
    assert reports == [("p1", "failed")]
    assert released == ["p1"]
    assert await source.acquire(request(exclude=frozenset({"p1"}))) is None
