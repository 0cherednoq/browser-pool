"""Неудачное открытие контекста: один вход на поколение, одна пауза, сбой хранилища не роняет вход.

Планировщик выдаёт несколько аренд identity раньше, чем её контекст открыт физически. Открывает
его первая; если не вышло, остальные получают ту же ошибку, а не входят на сайт по очереди.
"""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest

from browser_pool import (
    BaseFlow,
    BrowserPool,
    ErrorKind,
    Identity,
    OpenRequest,
    PoolConfig,
    PoolSignal,
    clock,
)
from browser_pool.config import Backoff, Limits, Recovery, Topology
from browser_pool.driver import LaunchSpec
from browser_pool.errors import IdentityBlockedError
from browser_pool.events import IdentityBlocked, OpenFailed, StateSaveFailed
from browser_pool.state import IdentityRecord, MemoryStateStore
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

A = Identity(key="mail:a", max_pages=4)
PAUSE = 30.0


class Login(BaseFlow[FakeContext, FakePage, str]):
    """Вход, который падает заданной ошибкой первые `failures` раз."""

    def __init__(self, error: Exception | None = None, *, failures: int = 1_000_000) -> None:
        self.error = error
        self.failures = failures
        self.opens = 0
        self.closed: list[str] = []

    @override
    async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> str:
        self.opens += 1
        await asyncio.sleep(1.0)  # вход не мгновенный: остальные аренды уже ждут
        if self.error is not None and self.opens <= self.failures:
            raise self.error
        return ctx.identity.key

    @override
    async def close(self, session: str) -> None:
        self.closed.append(session)


def make_pool(driver: FakeDriver, flow: Login | None = None, **parts: Any) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            recovery=Recovery(open_failure_backoff=Backoff.fixed(PAUSE)),
        ),
        flow=flow,
        **parts,
    )


async def lease_once(pool: Pool) -> str:
    try:
        async with pool.page(A, acquire_timeout=5.0):
            return "ok"
    except Exception as error:  # noqa: BLE001 — тест собирает исход каждой аренды
        return type(error).__name__


async def test_failed_login_is_not_repeated_by_leases_already_granted(
    fake_driver: FakeDriver,
) -> None:
    flow = Login(RuntimeError("пароль не подошёл"))
    pool = make_pool(fake_driver, flow)
    failed: list[OpenFailed] = []
    pool.on(OpenFailed, failed.append)

    async with pool:
        outcomes = await asyncio.gather(*(lease_once(pool) for _ in range(4)))

        assert flow.opens == 1
        assert outcomes == ["RuntimeError"] * 4
        assert len(failed) == 1
        assert pool.identity_status(A).open_failures == 1
        assert pool.snapshot().counters.open_failures == 1


async def test_blocked_login_is_tried_once(fake_driver: FakeDriver) -> None:
    flow = Login(PoolSignal("аккаунт забанен", kind=ErrorKind.blocked))
    pool = make_pool(fake_driver, flow)
    blocked: list[IdentityBlocked] = []
    pool.on(IdentityBlocked, blocked.append)

    async with pool:
        outcomes = await asyncio.gather(*(lease_once(pool) for _ in range(4)))

        assert flow.opens == 1
        assert outcomes == ["PoolSignal"] * 4
        assert len(blocked) == 1
        with pytest.raises(IdentityBlockedError):
            async with pool.page(A):
                pass


async def test_next_attempt_after_the_pause_logs_in_again(fake_driver: FakeDriver) -> None:
    flow = Login(RuntimeError("сайт лежит"), failures=1)

    async with make_pool(fake_driver, flow) as pool:
        await asyncio.gather(*(lease_once(pool) for _ in range(2)))
        started = clock.monotonic()

        async with pool.page(A):
            waited = clock.monotonic() - started

        assert flow.opens == 2
        assert waited >= PAUSE - 1.0


async def test_cancelled_opener_does_not_fail_the_others(fake_driver: FakeDriver) -> None:
    flow = Login()

    async with make_pool(fake_driver, flow) as pool:
        first = asyncio.create_task(lease_once(pool))
        await asyncio.sleep(0.5)  # первая аренда входит
        second = asyncio.create_task(lease_once(pool))
        await asyncio.sleep(0.1)
        first.cancel()

        assert await second == "ok"
        assert flow.opens == 2  # отмена — не сбой входа: вторая вошла сама
        await asyncio.gather(first, return_exceptions=True)


async def test_failed_launch_is_not_repeated_by_leases_already_granted(
    fake_driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = 0

    async def slow_broken_launch(spec: LaunchSpec) -> FakeBrowser:
        nonlocal launches
        _ = spec
        launches += 1
        await asyncio.sleep(1.0)  # запуск не мгновенный: остальные аренды уже ждут
        msg = "chrome не стартует"
        raise RuntimeError(msg)

    monkeypatch.setattr(fake_driver, "launch", slow_broken_launch)

    async with make_pool(fake_driver) as pool:
        outcomes = await asyncio.gather(*(lease_once(pool) for _ in range(4)))

        assert outcomes == ["RuntimeError"] * 4
        assert launches == 1
        assert pool.identity_status(A).open_failures == 1


# --- хранилище состояния ----------------------------------------------------------------


class BrokenStore(MemoryStateStore):
    """Хранилище, запись в которое падает."""

    @override
    async def save(self, record: IdentityRecord) -> IdentityRecord:
        msg = "диск недоступен"
        raise OSError(msg)


async def test_store_failure_after_login_does_not_fail_the_lease(fake_driver: FakeDriver) -> None:
    flow = Login()
    pool = make_pool(fake_driver, flow, state_store=BrokenStore())
    unsaved: list[StateSaveFailed] = []
    pool.on(StateSaveFailed, unsaved.append)

    async with pool:
        async with pool.page(A):
            pass
        async with pool.page(A):
            pass

        assert flow.opens == 1
        assert pool.identity_status(A).open_failures == 0
        assert [(event.key, event.trigger, event.error) for event in unsaved] == [
            ("mail:a", "open", "OSError")
        ]


class StuckStore(MemoryStateStore):
    """Хранилище, запись в которое не возвращается."""

    @override
    async def save(self, record: IdentityRecord) -> IdentityRecord:
        await asyncio.Event().wait()
        return record


async def test_cancel_while_saving_after_login_closes_the_session(fake_driver: FakeDriver) -> None:
    flow = Login()

    async with make_pool(fake_driver, flow, state_store=StuckStore()) as pool:
        task = asyncio.create_task(lease_once(pool))
        await asyncio.sleep(2.0)  # вход прошёл, идёт сохранение
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert flow.closed == ["mail:a"]  # сессию открыли — её и закрыли
        assert fake_driver.live.contexts == 0
