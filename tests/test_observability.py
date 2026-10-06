"""Наблюдаемость: события, снимок, watchdog, утечки аренд, логи — и ни одного секрета."""

from __future__ import annotations

import asyncio
import logging

import pytest

from browser_pool import BrowserPool, ErrorKind, Identity, PoolConfig, PoolSignal, ProxyPolicy
from browser_pool.config import (
    Lifecycle,
    Limits,
    Recycling,
    Timeouts,
    Topology,
)
from browser_pool.errors import LeaseRevokedError, PoolError
from browser_pool.events import (
    AcquireWatchdog,
    BrowserQuarantined,
    BrowserStarted,
    ContextClosed,
    ContextOpened,
    IdentityBlocked,
    LeakSuspected,
    LeaseAcquired,
    LeaseReleased,
    LeaseRevoked,
    PoolEvent,
    PoolHealth,
)
from browser_pool.proxies import Proxy
from browser_pool.snapshot import BrowserState
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

SECRETS = ("PAYLOAD-SECRET", "PROXY-SECRET", "COOKIE-SECRET")
A = Identity(
    key="mail:a",
    payload={"password": "PAYLOAD-SECRET"},
    proxy=ProxyPolicy.fixed(Proxy(host="1.2.3.4", port=80, username="u", password="PROXY-SECRET")),
    labels={"service": "mail"},
)
B = Identity(key="mail:b")


def make_pool(
    driver: FakeDriver,
    *,
    recycling: Recycling | None = None,
    limits: Limits | None = None,
    pages: int = 4,
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=pages, warm_pages_per_identity=1),
            limits=limits or Limits(spawn_delay=0.0, leak_warn_after=100.0),
            lifecycle=Lifecycle(healthcheck_interval=15.0),
            recycling=recycling or Recycling(browser_max_leases=None, browser_max_age=None),
            timeouts=Timeouts(close=2.0, acquire_watchdog=10.0),
        ),
    )


def record(pool: Pool) -> list[PoolEvent]:
    events: list[PoolEvent] = []
    pool.on(PoolEvent, events.append)
    return events


def of[E: PoolEvent](events: list[PoolEvent], kind: type[E]) -> list[E]:
    return [event for event in events if isinstance(event, kind)]


# --- события ---------------------------------------------------------------------------


async def test_lease_lifecycle_is_told_as_events(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    events = record(pool)

    async with pool:
        async with pool.page(A) as lease:
            lease_id = lease.lease_id
        await asyncio.sleep(0)

    started = of(events, BrowserStarted)
    opened = of(events, ContextOpened)
    acquired = of(events, LeaseAcquired)
    released = of(events, LeaseReleased)
    assert [event.browser_id for event in started] == ["browser-0"]
    assert [(event.key, event.generation) for event in opened] == [("mail:a", 1)]
    assert [(event.lease_id, event.key) for event in acquired] == [(lease_id, "mail:a")]
    assert [(event.lease_id, event.outcome) for event in released] == [(lease_id, None)]
    assert all(event.at.tzinfo is not None for event in events)


async def test_failures_are_told_with_their_kind(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    events = record(pool)

    async with pool:
        with pytest.raises(PoolSignal):
            async with pool.page(B):
                raise PoolSignal(kind=ErrorKind.blocked)
        await asyncio.sleep(0)

    (released,) = of(events, LeaseReleased)
    assert released.outcome is ErrorKind.blocked
    assert released.error == "PoolSignal"
    assert [event.key for event in of(events, IdentityBlocked)] == ["mail:b"]
    assert [event.key for event in of(events, ContextClosed)] == ["mail:b"]


async def test_crash_is_told(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    quarantined: list[BrowserQuarantined] = []
    pool.on(BrowserQuarantined, quarantined.append)

    async with pool:
        async with pool.page(B) as lease:
            browser = lease.browser
        fake_driver.crash(browser)
        await asyncio.sleep(1)

    assert [event.browser_id for event in quarantined] == ["browser-0"]


async def test_failing_handler_does_not_break_the_pool(
    fake_driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    pool = make_pool(fake_driver)

    def explode(event: PoolEvent) -> None:
        raise RuntimeError(type(event).__name__)

    pool.on(LeaseAcquired, explode)

    with caplog.at_level(logging.ERROR, logger="browser_pool"):
        async with pool, pool.page(B) as lease:
            assert lease.page.alive

    assert "LeaseAcquired" in caplog.text


async def test_async_handlers_are_awaited_by_the_pool(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    seen: list[str] = []

    async def slow(event: LeaseReleased) -> None:
        await asyncio.sleep(5)
        seen.append(event.key)

    pool.on(LeaseReleased, slow)
    async with pool, pool.page(B):
        pass

    assert seen == ["mail:b"]  # остановка дождалась обработчика


async def test_unsubscribe(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    events: list[PoolEvent] = []
    unsubscribe = pool.on(LeaseAcquired, events.append)
    unsubscribe()

    async with pool, pool.page(B):
        pass

    assert events == []


# --- снимок ----------------------------------------------------------------------------


async def test_snapshot_shows_capacity_leases_and_waiters(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, pages=1) as pool:
        release = asyncio.Event()

        async def hold() -> None:
            async with pool.page(A):
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(hold())
        await asyncio.sleep(7)

        snapshot = pool.snapshot()

        assert snapshot.capacity_total == 1
        assert snapshot.capacity_healthy == 1
        assert snapshot.leases_active == 1
        assert snapshot.waiting == 1
        assert snapshot.oldest_waiter_seconds == pytest.approx(7)
        assert snapshot.oldest_waiter_candidates == ("mail:a",)
        assert snapshot.browsers[0].state is BrowserState.healthy
        assert snapshot.browsers[0].launched
        assert [(context.key, context.active) for context in snapshot.contexts] == [("mail:a", 1)]
        assert snapshot.counters.acquired == 1

        release.set()
        await asyncio.gather(holder, waiter)
        after = pool.snapshot()
        assert after.counters.acquired == 2
        assert after.counters.wait_max == pytest.approx(7)


async def test_health_is_published_periodically(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    health: list[PoolHealth] = []
    pool.on(PoolHealth, health.append)

    async with pool:
        await asyncio.sleep(31)

    assert len(health) == 2
    assert health[0].snapshot.capacity_total == 4


# --- долгое ожидание и утечки ----------------------------------------------------------


async def test_long_wait_is_reported_and_keeps_waiting(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver, pages=1)
    watchdogs: list[AcquireWatchdog] = []
    pool.on(AcquireWatchdog, watchdogs.append)

    async with pool:
        release = asyncio.Event()

        async def hold() -> None:
            async with pool.page(B):
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(hold())
        await asyncio.sleep(25)
        release.set()
        await asyncio.gather(holder, waiter)

    assert [round(event.waited) for event in watchdogs] == [10, 20]
    assert watchdogs[0].candidates == ("mail:b",)


async def test_long_lease_is_suspected_with_the_place_it_was_taken(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    leaks: list[LeakSuspected] = []
    pool.on(LeakSuspected, leaks.append)

    async with pool, pool.page(B):
        await asyncio.sleep(150)

    (leak,) = leaks
    assert leak.key == "mail:b"
    assert leak.held == pytest.approx(100)
    assert "test_long_lease_is_suspected" in leak.acquired_at


async def test_lease_over_its_maximum_is_revoked(fake_driver: FakeDriver) -> None:
    recycling = Recycling(browser_max_leases=None, browser_max_age=None)
    limits = Limits(spawn_delay=0.0, leak_warn_after=100.0, lease_max_duration=200.0)
    pool = make_pool(fake_driver, recycling=recycling, limits=limits)
    revoked: list[LeaseRevoked] = []
    pool.on(LeaseRevoked, revoked.append)

    async with pool:

        async def forever() -> None:
            async with pool.page(B):
                await asyncio.sleep(10_000)

        holder = asyncio.create_task(forever())
        with pytest.raises(LeaseRevokedError) as caught:
            await holder

        assert caught.value.identity == "mail:b"
        assert caught.value.held == pytest.approx(200.0)
        assert pool.snapshot().leases_active == 0
    assert [event.key for event in revoked] == ["mail:b"]


async def test_revoked_lease_does_not_cancel_the_task_around_it(fake_driver: FakeDriver) -> None:
    limits = Limits(spawn_delay=0.0, leak_warn_after=None, lease_max_duration=200.0)
    seen: list[object] = []

    async def holder(pool: Pool) -> str:
        task = asyncio.current_task()
        assert task is not None
        try:
            async with pool.page(B):
                await asyncio.sleep(10_000)
        except PoolError as error:  # ошибка пула, а не отмена: задача жива и работает дальше
            seen.append(type(error))
        seen.append(task.cancelling())
        await asyncio.sleep(1.0)
        return "дожила"

    async with make_pool(fake_driver, limits=limits) as pool:
        async with asyncio.TaskGroup() as group:  # отзыв аренды соседей по группе не отменяет
            first = group.create_task(holder(pool))
            other = group.create_task(asyncio.sleep(300.0, "сосед цел"))

        assert first.result() == "дожила"
        assert other.result() == "сосед цел"
        assert seen == [LeaseRevokedError, 0]


async def test_cancel_from_outside_wins_over_the_revoke(fake_driver: FakeDriver) -> None:
    limits = Limits(spawn_delay=0.0, leak_warn_after=None, lease_max_duration=200.0)

    async def stubborn(pool: Pool) -> None:
        async with pool.page(B):
            try:
                await asyncio.sleep(10_000)
            finally:
                await asyncio.sleep(
                    50.0
                )  # отзыв пришёл, арендатор ещё прибирается — и тут его отменяют

    async with make_pool(fake_driver, limits=limits) as pool:
        task = asyncio.create_task(stubborn(pool))
        await asyncio.sleep(220.0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.snapshot().leases_active == 0


# --- секреты ---------------------------------------------------------------------------


async def test_no_secret_reaches_events_snapshot_or_logs(
    fake_driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    pool = make_pool(fake_driver)
    events = record(pool)

    async def fail_session() -> None:
        async with pool.page(A):
            raise PoolSignal(kind=ErrorKind.session)

    with caplog.at_level(logging.DEBUG, logger="browser_pool"):
        async with pool:
            async with pool.page(A):
                pass
            with pytest.raises(PoolSignal):
                await fail_session()
            await asyncio.sleep(20)
            snapshot = pool.snapshot()

    rendered = "\n".join([*(repr(event) for event in events), repr(snapshot), caplog.text])
    for secret in SECRETS:
        assert secret not in rendered
    assert "mail:a" in rendered
