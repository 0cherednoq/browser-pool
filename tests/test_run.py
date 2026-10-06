"""`pool.run` / `pool.map` и `Backoff`: новая аренда на попытку, повтор по виду сбоя."""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest

from browser_pool import Backoff, BaseFlow, BrowserPool, ErrorKind, Identity, PoolConfig, PoolSignal
from browser_pool.config import Limits, Recovery, Topology
from browser_pool.errors import (
    AcquireTimeoutError,
    ConfigError,
    IdentityBlockedError,
    IdentityCoolingDownError,
)
from browser_pool.events import TaskRetried
from browser_pool.flow import OpenRequest
from browser_pool.identity import ProxyPolicy
from browser_pool.lease import PageLease
from browser_pool.proxies import Proxy
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage
from browser_pool.testing.fake_driver import FakeTargetClosedError
from browser_pool.testing.fake_http import FakeProxyError

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]
type Lease = PageLease[FakeBrowser, FakeContext, FakePage, Any]

A = Identity(key="mail:a")
B = Identity(key="mail:b")


def make_pool(driver: FakeDriver, **options: object) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            recovery=Recovery(
                open_failure_backoff=Backoff(initial=5.0, maximum=900.0, factor=2.0, jitter=0.0)
            ),
        ),
        **options,  # pyright: ignore[reportArgumentType] — flow и прочее
    )


class Flaky:
    """Задача, которая первые `failures` раз падает с `error`."""

    def __init__(self, error: Exception, failures: int = 1) -> None:
        self.error = error
        self.failures = failures
        self.pages: list[FakePage] = []

    async def __call__(self, lease: Lease) -> str:
        self.pages.append(lease.page)
        if len(self.pages) <= self.failures:
            raise self.error
        return lease.identity.key


# --- run --------------------------------------------------------------------------------


async def test_run_returns_what_the_task_returns(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        assert await pool.run(Flaky(ValueError(), failures=0), A) == "mail:a"


async def test_page_fault_is_retried_on_a_fresh_page_after_the_pause(
    fake_driver: FakeDriver,
) -> None:
    task = Flaky(FakeTargetClosedError("вкладка умерла"))
    retried: list[TaskRetried] = []

    async with make_pool(fake_driver) as pool:
        pool.on(TaskRetried, retried.append)
        started = asyncio.get_running_loop().time()
        result = await pool.run(task, A, retries=2, backoff=Backoff.fixed(2.0))
        waited = asyncio.get_running_loop().time() - started

    assert result == "mail:a"
    assert task.pages[0] is not task.pages[1]  # упавшая вкладка выброшена
    assert [(event.attempt, event.kind, event.delay) for event in retried] == [(1, "page", 2.0)]
    assert waited >= 2.0


async def test_committed_attempt_is_not_retried(fake_driver: FakeDriver) -> None:
    attempts: list[Lease] = []

    async def task(lease: Lease) -> str:
        attempts.append(lease)
        lease.commit()
        msg = "вкладка умерла после отправки"
        raise FakeTargetClosedError(msg)

    async with make_pool(fake_driver) as pool:
        with pytest.raises(FakeTargetClosedError):
            await pool.run(task, A, retries=3, backoff=Backoff.none())

    assert len(attempts) == 1
    assert attempts[0].committed


async def test_attempt_before_commit_is_still_retried(fake_driver: FakeDriver) -> None:
    attempts: list[Lease] = []

    async def task(lease: Lease) -> str:
        attempts.append(lease)
        if len(attempts) == 1:
            msg = "вкладка умерла до отправки"
            raise FakeTargetClosedError(msg)
        lease.commit()
        return lease.identity.key

    async with make_pool(fake_driver) as pool:
        assert await pool.run(task, A, retries=1, backoff=Backoff.none()) == "mail:a"

    assert [lease.committed for lease in attempts] == [False, True]


async def test_reported_kind_decides_the_retry(fake_driver: FakeDriver) -> None:
    calls: list[int] = []

    async def task(lease: Lease) -> int:
        calls.append(lease.generation)
        if len(calls) == 1:
            lease.report(ErrorKind.session)
            msg = "следствие"
            raise RuntimeError(msg)
        return lease.generation

    async with make_pool(fake_driver) as pool:
        assert await pool.run(task, A, retries=1, backoff=Backoff.none()) == 2

    assert calls == [1, 2]  # сессия протухла: контекст пересоздан


@pytest.mark.parametrize(
    "error",
    [ValueError("баг задачи"), PoolSignal("бан", kind=ErrorKind.blocked)],
    ids=["unknown", "blocked"],
)
async def test_task_and_identity_faults_are_not_retried(
    fake_driver: FakeDriver, error: Exception
) -> None:
    task = Flaky(error, failures=5)

    async with make_pool(fake_driver) as pool:
        with pytest.raises(type(error)):
            await pool.run(task, A, retries=3, backoff=Backoff.none())

    assert len(task.pages) == 1


async def test_blocked_identity_is_not_waited_for(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        with pytest.raises(PoolSignal):
            await pool.run(Flaky(PoolSignal("бан", kind="blocked")), A, retries=3)
        with pytest.raises(IdentityBlockedError):
            await pool.run(Flaky(ValueError(), failures=0), A, retries=3)


async def test_last_error_is_raised_when_attempts_run_out(fake_driver: FakeDriver) -> None:
    task = Flaky(FakeTargetClosedError("снова"), failures=10)

    async with make_pool(fake_driver) as pool:
        with pytest.raises(FakeTargetClosedError, match="снова"):
            await pool.run(task, A, retries=2, backoff=Backoff.none())

    assert len(task.pages) == 3


async def test_retry_on_widens_what_is_retried(fake_driver: FakeDriver) -> None:
    retry_on = {ErrorKind.unknown}
    async with make_pool(fake_driver) as pool:
        result = await pool.run(Flaky(ValueError()), A, retries=1, retry_on=retry_on)

    assert result == "mail:a"


async def test_pool_errors_are_not_retried(fake_driver: FakeDriver) -> None:
    release = asyncio.Event()

    async def hog(lease: Lease) -> None:
        _ = lease
        await release.wait()

    pool = BrowserPool(
        fake_driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=1), limits=Limits(spawn_delay=0.0)
        ),
    )
    async with pool:
        holder = asyncio.create_task(pool.run(hog, A))
        await asyncio.sleep(0.1)
        started = asyncio.get_running_loop().time()
        with pytest.raises(AcquireTimeoutError):
            await pool.run(Flaky(ValueError(), failures=0), B, retries=5, acquire_timeout=1.0)
        assert asyncio.get_running_loop().time() - started < 2.0  # одна попытка, не шесть
        release.set()
        await holder


async def test_run_and_map_do_not_wait_for_a_cooling_identity_when_told_so(
    fake_driver: FakeDriver,
) -> None:
    async def task(lease: Lease) -> str:
        return lease.identity.key

    async with make_pool(fake_driver) as pool:
        pool.cool_down("mail:a", 60.0)
        with pytest.raises(IdentityCoolingDownError):
            await pool.run(task, A, wait_cooldown=False)
        with pytest.raises(IdentityCoolingDownError):
            await pool.map(task, [A], wait_cooldown=False)


async def test_map_limits_the_wait_for_a_slot(fake_driver: FakeDriver) -> None:
    release = asyncio.Event()

    async def hog(_lease: Lease) -> None:
        await release.wait()

    async def task(lease: Lease) -> str:
        return lease.identity.key

    pool = BrowserPool(
        fake_driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=1), limits=Limits(spawn_delay=0.0)
        ),
    )
    async with pool:
        holder = asyncio.create_task(pool.run(hog, A))
        await asyncio.sleep(0.1)
        results = await pool.map(task, [B], acquire_timeout=0.5, return_exceptions=True)
        assert isinstance(results[0], AcquireTimeoutError)
        release.set()
        await holder


async def test_proxy_that_failed_the_login_is_retried(fake_driver: FakeDriver) -> None:
    logins: list[int] = []

    class Login(BaseFlow[FakeContext, FakePage, None]):
        @override
        async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> None:
            _ = ctx
            logins.append(1)
            if len(logins) == 1:
                msg = "прокси не пустил"
                raise FakeProxyError(msg)

    identity = Identity(key="mail:p", proxy=ProxyPolicy.fixed(Proxy(host="10.0.0.1", port=8080)))
    async with make_pool(fake_driver, flow=Login()) as pool:
        result = await pool.run(Flaky(ValueError(), failures=0), identity, retries=1)

    assert result == "mail:p"
    assert len(logins) == 2  # ProxyFailedError — повтор, после паузы открытия


async def test_negative_retries_are_rejected(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        with pytest.raises(ValueError, match="retries"):
            await pool.run(Flaky(ValueError(), failures=0), A, retries=-1)


# --- map --------------------------------------------------------------------------------


async def test_map_keeps_order_and_concurrency(fake_driver: FakeDriver) -> None:
    identities = [Identity(key=f"mail:{index}") for index in range(6)]
    running = 0
    peak = 0

    async def task(lease: Lease) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1
        return lease.identity.key

    pool = BrowserPool(
        fake_driver,
        config=PoolConfig(topology=Topology(browsers=2), limits=Limits(spawn_delay=0.0)),
    )
    async with pool:
        results = await pool.map(task, identities, concurrency=3)

    assert results == [identity.key for identity in identities]
    assert peak == 3


async def test_map_failure_cancels_the_rest_and_is_raised_plainly(
    fake_driver: FakeDriver,
) -> None:
    finished: list[str] = []

    async def task(lease: Lease) -> str:
        if lease.identity is B:
            msg = "упало"
            raise ValueError(msg)
        await asyncio.sleep(10)
        finished.append(lease.identity.key)
        return lease.identity.key

    async with make_pool(fake_driver) as pool:
        with pytest.raises(ValueError, match="упало"):
            await pool.map(task, [A, B])

    assert finished == []


async def test_map_can_return_exceptions_in_place(fake_driver: FakeDriver) -> None:
    async def task(lease: Lease) -> str:
        if lease.identity is B:
            msg = "упало"
            raise ValueError(msg)
        return lease.identity.key

    async with make_pool(fake_driver) as pool:
        first, second = await pool.map(task, [A, B], return_exceptions=True)

    assert first == "mail:a"
    assert isinstance(second, ValueError)


# --- Backoff ----------------------------------------------------------------------------


def test_exponential_backoff_grows_to_its_ceiling() -> None:
    backoff = Backoff.exp(1, 30, jitter=0.0)

    assert [backoff.delay(attempt) for attempt in range(7)] == [1, 2, 4, 8, 16, 30, 30]


def test_jitter_spreads_both_ways_but_not_over_the_ceiling() -> None:
    backoff = Backoff.exp(10, 12, jitter=0.1)

    assert backoff.delay(0, spread=0.0) == pytest.approx(9.0)
    assert backoff.delay(0, spread=1.0) == pytest.approx(11.0)
    assert backoff.delay(3, spread=1.0) == 12


def test_fixed_and_none() -> None:
    assert [Backoff.fixed(3).delay(attempt) for attempt in range(3)] == [3, 3, 3]
    assert Backoff.none().delay(5) == 0


@pytest.mark.parametrize(
    "values",
    [{"initial": -1.0}, {"initial": 5.0, "maximum": 1.0}, {"factor": 0.5}, {"jitter": 2.0}],
    ids=["negative", "ceiling", "factor", "jitter"],
)
def test_bad_backoff_is_rejected(values: dict[str, float]) -> None:
    with pytest.raises(ConfigError):
        Backoff(**values)
