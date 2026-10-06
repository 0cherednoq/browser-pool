"""`ProxyList` (M8.08): резидентные шлюзы, одно срабатывание на инцидент, номера строк, здоровье и правка на лету."""

from __future__ import annotations

import asyncio

import pytest

from browser_pool.proxies import (
    Proxy,
    ProxyFormatError,
    ProxyLease,
    ProxyList,
    ProxyOutcome,
    ProxyRequest,
)


def request(key: str = "a") -> ProxyRequest:
    return ProxyRequest(identity_key=key)


async def take(source: ProxyList, key: str = "a") -> ProxyLease:
    lease = await source.acquire(request(key))
    assert lease is not None
    return lease


# --- идентичность по кредам ------------------------------------------------------------

GATE = [
    "gate.example.com:7000:user-session-1:pw1",
    "gate.example.com:7000:user-session-2:pw2",
    "gate.example.com:7000:user-session-3:pw3",
]


def test_residential_sessions_on_one_gateway_are_different_proxies() -> None:
    source = ProxyList.parse_lines(GATE)

    names = source.labels()

    assert len(set(names)) == 3
    for name in names:
        assert name.startswith("http://gate.example.com:7000#")
        assert "user-session" not in name
        assert "pw" not in name


def test_label_of_a_proxy_without_login_is_still_its_address() -> None:
    assert Proxy(host="1.2.3.4", port=80).label == "http://1.2.3.4:80"
    assert Proxy(host="1.2.3.4", port=80, username="u", id="db-1").label == "db-1"


def test_same_login_and_password_in_two_notations_is_one_proxy() -> None:
    source = ProxyList.parse_lines(
        ["gate.example.com:7000:ada:pw", "http://ada:pw@gate.example.com:7000"]
    )

    assert len(source.labels()) == 1


def test_same_login_with_different_passwords_needs_an_explicit_id() -> None:
    with pytest.raises(ValueError, match="повторяются"):
        ProxyList.parse_lines(["gate.example.com:7000:ada:pw1", "gate.example.com:7000:ada:pw2"])


# --- выключатель -----------------------------------------------------------------------


async def test_late_reports_of_one_incident_trip_the_breaker_once() -> None:
    source = ProxyList(
        [Proxy(host="1.2.3.4", port=80, id="p1")], breaker_failures=3, breaker_cooldown=600.0
    )
    lease = await take(source)

    for _ in range(15):
        await source.report(lease, ProxyOutcome.failed("timeout"))

    (status,) = source.status()
    assert status.trips == 1
    assert status.resting_for == pytest.approx(600.0)
    await asyncio.sleep(601)
    assert await source.acquire(request("next")) is not None  # пауза 600, а не 9600 с


async def test_banned_reports_during_the_pause_do_not_extend_it() -> None:
    source = ProxyList([Proxy(host="1.2.3.4", port=80, id="p1")], breaker_cooldown=100.0)
    lease = await take(source)

    await source.report(lease, ProxyOutcome.banned("403"))
    await source.report(lease, ProxyOutcome.banned("403"))

    await asyncio.sleep(101)
    assert await source.acquire(request("next")) is not None


async def test_ban_from_application_code_rests_the_proxy() -> None:
    proxy = Proxy(host="1.2.3.4", port=80, id="p1")
    source = ProxyList([proxy, Proxy(host="1.2.3.5", port=80, id="p2")], strategy="round_robin")

    await source.ban(proxy, reason="сайт отдал капчу")
    await source.ban("p1")  # повтор безвреден

    assert (await take(source)).proxy_id == "p2"
    assert [s.name for s in source.status() if s.resting_for is not None] == ["p1"]


# --- строки ----------------------------------------------------------------------------


def test_error_names_the_line_number_and_not_its_content() -> None:
    lines = ["# список", "1.2.3.4:8080", "", "user:Zq~secret:1.2.3.4:notaport"]

    with pytest.raises(ProxyFormatError, match=r"строка 4") as caught:
        ProxyList.parse_lines(lines)

    assert "Zq~secret" not in str(caught.value)


def test_invalid_lines_can_be_skipped_with_their_numbers() -> None:
    lines = ["1.2.3.4:8080", "это не прокси", "5.6.7.8:3128", "host:99999"]

    source = ProxyList.parse_lines(lines, skip_invalid=True)

    assert len(source.labels()) == 2
    assert source.skipped_lines == (2, 4)


def test_skipping_everything_leaves_an_empty_list_error() -> None:
    with pytest.raises(ValueError, match="пуст"):
        ProxyList.parse_lines(["не прокси"], skip_invalid=True)


# --- правка на лету --------------------------------------------------------------------


async def test_entries_can_be_added_and_removed_while_the_list_is_in_use() -> None:
    first = Proxy(host="1.2.3.4", port=80, id="p1")
    source = ProxyList([first], strategy="round_robin")
    lease = await take(source)

    source.add(Proxy(host="1.2.3.5", port=80, id="p2"))
    source.remove("p1")

    assert source.labels() == ("p2",)
    assert (await take(source, "b")).proxy_id == "p2"
    await source.release(lease)  # контекст на удалённом прокси дорабатывает


def test_the_last_entry_cannot_be_removed_and_duplicates_cannot_be_added() -> None:
    source = ProxyList([Proxy(host="1.2.3.4", port=80, id="p1")])

    with pytest.raises(ValueError, match="хотя бы одна"):
        source.remove("p1")
    with pytest.raises(ValueError, match="уже есть"):
        source.add(Proxy(host="9.9.9.9", port=80, id="p1"))
