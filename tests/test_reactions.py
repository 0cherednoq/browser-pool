"""Реакции на виды сбоев и статусы identity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from browser_pool import BrowserPool, ErrorKind, Identity, PoolConfig, PoolSignal, clock
from browser_pool.config import (
    Backoff,
    Limits,
    Recovery,
    Recycling,
    Timeouts,
    Topology,
)
from browser_pool.errors import Classification, IdentityBlockedError, IdentityCoolingDownError
from browser_pool.snapshot import BrowserState
from browser_pool.testing import (
    FakeBrowser,
    FakeBrowserCrashedError,
    FakeContext,
    FakeDriver,
    FakePage,
)

A = Identity(key="mail:a")
B = Identity(key="mail:b")

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


class BannedError(Exception):
    """Ошибка приложения, которую пул узнаёт через свой classify."""


class ThrottledError(Exception):
    """Ошибка приложения с паузой: classify отдаёт её пулу через `Classification`."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"подождите {retry_after} с")
        self.retry_after = retry_after


class SdkError(Exception):
    """Ошибка site SDK, в которую он заворачивает причину — сам вида не объявляет."""


def classify(error: BaseException) -> ErrorKind | Classification | None:
    if isinstance(error, BannedError):
        return ErrorKind.blocked
    if isinstance(error, ThrottledError):
        return Classification(ErrorKind.rate_limited, error.retry_after)
    return None


def wrapped(cause: BaseException) -> SdkError:
    """`raise SdkError(...) from cause` — без raise."""
    error = SdkError("операция не удалась")
    error.__cause__ = cause
    return error


def make_pool(driver: FakeDriver, *, recycling: Recycling | None = None) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4, warm_pages_per_identity=2),
            limits=Limits(spawn_delay=0.0),
            recycling=recycling or Recycling(browser_max_leases=None, browser_max_age=None),
            recovery=Recovery(
                open_failure_backoff=Backoff(initial=30.0, maximum=100.0, factor=2.0, jitter=0.0),
                rate_limited_cooldown=60.0,
            ),
            timeouts=Timeouts(close=2.0),
        ),
        classifier=classify,
    )


async def lease_generation(pool: Pool, identity: Identity) -> int:
    async with pool.page(identity) as lease:
        return lease.generation


async def fail_with(pool: Pool, identity: Identity, error: BaseException) -> FakePage:
    pages: list[FakePage] = []

    async def fail_inside() -> None:
        async with pool.page(identity) as lease:
            pages.append(lease.page)
            raise error

    with pytest.raises(type(error)):
        await fail_inside()
    await asyncio.sleep(0)
    return pages[0]


# --- вкладка и контекст ----------------------------------------------------------------


async def test_unknown_error_costs_only_the_page(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        page = await fail_with(pool, A, RuntimeError("что-то"))

        assert not page.alive
        assert await lease_generation(pool, A) == 1


async def test_reported_page_is_discarded_without_an_error(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            lease.report(ErrorKind.page)
            page = lease.page

        assert not page.alive
        assert await lease_generation(pool, A) == 1


@pytest.mark.parametrize(
    "error",
    [PoolSignal("вышли из ящика", kind=ErrorKind.session), PoolSignal(kind="proxy")],
    ids=["session", "proxy"],
)
async def test_session_and_proxy_failures_replace_the_context(
    fake_driver: FakeDriver, error: PoolSignal
) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, error)

        assert await lease_generation(pool, A) == 2


@dataclass
class ForeignSessionError(Exception):
    """Ошибка чужого SDK, объявившая вид сбоя без импорта browser_pool."""

    pool_error_kind: str = "session"


async def test_foreign_declared_kind_is_honoured(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, ForeignSessionError())

        assert await lease_generation(pool, A) == 2


async def test_report_wins_over_the_error_that_follows(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:

        async def report_then_fail() -> None:
            async with pool.page(A) as lease:
                lease.report(ErrorKind.session)
                consequence = "следствие"
                raise RuntimeError(consequence)

        with pytest.raises(RuntimeError):
            await report_then_fail()
        await asyncio.sleep(0)

        assert await lease_generation(pool, A) == 2


async def test_signal_wrapped_by_the_sdk_is_still_read(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, wrapped(PoolSignal(kind=ErrorKind.session)))

        assert await lease_generation(pool, A) == 2


async def test_classifier_recognises_a_wrapped_cause(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, wrapped(BannedError("челлендж")))

        with pytest.raises(IdentityBlockedError):
            async with pool.page(A):
                pass


async def test_driver_recognises_a_wrapped_cause(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, wrapped(FakeBrowserCrashedError("упал")))

        await asyncio.sleep(1)
        assert sum(call.operation == "launch" for call in fake_driver.calls) == 2


# --- браузер ---------------------------------------------------------------------------


async def test_driver_classified_crash_quarantines_the_browser(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, FakeBrowserCrashedError("упал"))

        await asyncio.sleep(1)
        launches = sum(call.operation == "launch" for call in fake_driver.calls)
        assert launches == 2
        assert browser_states(pool) == {"browser-0": BrowserState.healthy}


# --- пауза -----------------------------------------------------------------------------


async def test_rate_limit_pauses_the_identity_and_keeps_its_context(
    fake_driver: FakeDriver,
) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            lease.report(ErrorKind.rate_limited, retry_after=40.0)
            page = lease.page
        started = clock.monotonic()

        assert page.alive  # без исключения вкладка остаётся тёплой
        with pytest.raises(IdentityCoolingDownError) as caught:
            async with pool.page(A, wait_cooldown=False):
                pass
        assert caught.value.retry_after == pytest.approx(40.0)

        async with pool.page(any_of=[A, B]) as lease:
            assert lease.identity.key == "mail:b"  # пока A на паузе — другой кандидат

        async with pool.page(A) as lease:
            assert clock.monotonic() - started == pytest.approx(40.0)
            assert lease.generation == 1


async def test_rate_limit_without_retry_after_takes_the_default(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, PoolSignal(kind=ErrorKind.rate_limited))
        started = clock.monotonic()

        async with pool.page(A):
            assert clock.monotonic() - started == pytest.approx(60.0)


async def test_classifier_passes_the_pause_it_read(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, ThrottledError(retry_after=25.0))
        started = clock.monotonic()

        async with pool.page(A) as lease:
            assert clock.monotonic() - started == pytest.approx(25.0)
            assert lease.generation == 1  # пауза не стоит контекста


async def test_manual_cooldown(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        pool.cool_down("mail:a", 30.0)
        started = clock.monotonic()

        async with pool.page(A):
            assert clock.monotonic() - started == pytest.approx(30.0)


async def test_status_methods_take_an_identity_or_its_key(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        pool.cool_down(A, 30.0)
        assert pool.identity_status(A).cooling_until == pool.identity_status("mail:a").cooling_until
        pool.block(A, "бан")
        assert pool.identity_status("mail:a").blocked == "бан"
        pool.unblock(A)
        assert pool.identity_status(A).blocked is None
        assert await pool.export_state(A) is None


async def test_shorter_cooldown_does_not_cut_and_unblock_lifts_it(fake_driver: FakeDriver) -> None:
    # docs/extending/flow.md, «Две паузы»: пауза только продлевается; снять досрочно — unblock.
    async with make_pool(fake_driver) as pool:
        pool.cool_down("mail:a", 100.0)
        pool.cool_down("mail:a", 1.0)
        assert pool.identity_status("mail:a").cooling_until is not None

        started = clock.monotonic()

        async def lease_a() -> float:
            async with pool.page(A):
                return clock.monotonic() - started

        waiter = asyncio.create_task(lease_a())
        await asyncio.sleep(10)
        assert not waiter.done()
        pool.unblock("mail:a")
        assert await waiter == pytest.approx(10.0)
        assert pool.identity_status("mail:a").cooling_until is None


# --- блокировка ------------------------------------------------------------------------


async def test_banned_identity_is_blocked_until_unblocked(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        await fail_with(pool, A, BannedError("челлендж"))

        with pytest.raises(IdentityBlockedError, match="mail:a"):
            async with pool.page(A):
                pass
        assert pool.identity_status("mail:a").blocked is not None

        pool.unblock("mail:a")
        assert await lease_generation(pool, A) == 2


async def test_waiter_for_an_identity_that_gets_blocked_is_released(
    fake_driver: FakeDriver,
) -> None:
    async with make_pool(fake_driver) as pool:
        pool.cool_down("mail:a", 100.0)

        async def wait_for_a() -> None:
            async with pool.page(A):
                pass

        waiter = asyncio.create_task(wait_for_a())
        await asyncio.sleep(1)
        pool.block("mail:a", "бан от оператора")

        with pytest.raises(IdentityBlockedError):
            await waiter


# --- неудачное открытие ----------------------------------------------------------------


async def test_failed_open_pauses_the_identity_with_growing_pauses(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        fake_driver.faults.fail("new_context", RuntimeError("не открылся"), times=2)

        with pytest.raises(RuntimeError, match="не открылся"):
            async with pool.page(A):
                pass
        first_pause = pool.identity_status("mail:a").cooling_until
        assert first_pause == pytest.approx(clock.monotonic() + 30.0)

        with pytest.raises(RuntimeError, match="не открылся"):
            async with pool.page(A):
                pass
        second_pause = pool.identity_status("mail:a").cooling_until
        assert second_pause == pytest.approx(clock.monotonic() + 60.0)

        async with pool.page(A) as lease:
            assert lease.page.alive
        assert pool.identity_status("mail:a").open_failures == 0


# --- счётчики контекста ----------------------------------------------------------------


async def test_context_is_retired_after_its_uses(fake_driver: FakeDriver) -> None:
    recycling = Recycling(browser_max_leases=None, browser_max_age=None, context_max_leases=2)
    async with make_pool(fake_driver, recycling=recycling) as pool:
        generations = [await lease_generation(pool, A) for _ in range(3)]
        await asyncio.sleep(0)

        assert generations == [1, 1, 2]


async def test_context_is_retired_when_its_error_score_runs_out(fake_driver: FakeDriver) -> None:
    recycling = Recycling(
        browser_max_leases=None, browser_max_age=None, context_max_error_score=2.0
    )
    async with make_pool(fake_driver, recycling=recycling) as pool:
        await fail_with(pool, A, RuntimeError("раз"))
        assert await lease_generation(pool, A) == 1  # успех снимает 0.5
        await fail_with(pool, A, RuntimeError("два"))
        await fail_with(pool, A, RuntimeError("три"))

        assert await lease_generation(pool, A) == 2


def browser_states(pool: BrowserPool[Any, Any, Any]) -> dict[str, BrowserState]:
    """Состояние каждого браузера — из снимка пула."""
    return {browser.id: browser.state for browser in pool.snapshot().browsers}
