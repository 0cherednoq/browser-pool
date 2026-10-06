"""Сессии в пуле: восстановление, вход один раз, сохранение в каждой точке, режимы, сбои."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import assert_type, override

import pytest

from browser_pool import (
    BrowserPool,
    ErrorKind,
    Identity,
    PageLease,
    PoolConfig,
    PoolSignal,
    StatePolicy,
    clock,
)
from browser_pool.config import (
    Lifecycle,
    Limits,
    Recycling,
    Timeouts,
    Topology,
)
from browser_pool.driver import DriverCapabilities
from browser_pool.errors import IdentityBlockedError
from browser_pool.events import SessionOpened, StateExportFailed, StateSaved
from browser_pool.flow import BaseFlow, OpenRequest
from browser_pool.state import (
    Cookie,
    IdentityRecord,
    MemoryStateStore,
    Origin,
    SessionState,
    StateStore,
)
from browser_pool.testing import FAKE_CAPABILITIES, FakeBrowser, FakeContext, FakeDriver, FakePage

A = Identity(key="mail:a", payload={"login": "ada"})
B = Identity(key="mail:b", payload={"login": "bob"})
SID = Cookie(name="sid", value="logged-in", domain=".mail.example")

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


@dataclass
class MailSession:
    """То, что возвращает вход."""

    login: str
    inbox: str


class MailFlow(BaseFlow[FakeContext, FakePage, MailSession]):
    """Site SDK почты на фейковом драйвере: входит, если восстановленная сессия не жива."""

    def __init__(self, driver: FakeDriver, *, login_takes: float = 0.0) -> None:
        self.driver = driver
        self.login_takes = login_takes
        self.logins: list[str] = []
        self.prepared: list[int] = []
        self.closed: list[str] = []
        self.reset_answer = True

    @override
    async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> MailSession:
        page = await ctx.new_page()
        page.url = "https://mail.example/login"
        login = str(ctx.identity.payload["login"])
        if ctx.restored is None or SID not in ctx.restored.cookies:
            await asyncio.sleep(self.login_takes)
            self.logins.append(login)
            await self.driver.add_cookies(ctx.context, [SID])
        return MailSession(login=login, inbox="https://mail.example/inbox")

    @override
    async def prepare_page(self, session: MailSession, page: FakePage) -> None:
        page.url = session.inbox
        self.prepared.append(page.id)

    @override
    async def reset_page(self, session: MailSession, page: FakePage) -> bool:
        return self.reset_answer

    @override
    async def close(self, session: MailSession) -> None:
        self.closed.append(session.login)


def make_pool(
    driver: FakeDriver,
    flow: MailFlow,
    *,
    store: StateStore | None = None,
    lifecycle: Lifecycle | None = None,
    recycling: Recycling | None = None,
    concurrent_opens: int = 4,
) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0, concurrent_opens=concurrent_opens),
            lifecycle=lifecycle or Lifecycle(),
            recycling=recycling or Recycling(browser_max_leases=None, browser_max_age=None),
            timeouts=Timeouts(open=30.0),
        ),
        flow=flow,
        state_store=store,
    )


# --- вход и восстановление -------------------------------------------------------------


async def test_session_opens_once_per_context_and_warms_pages(fake_driver: FakeDriver) -> None:
    flow = MailFlow(fake_driver)

    async with make_pool(fake_driver, flow) as pool:
        for _ in range(3):
            async with pool.page(A) as lease:
                assert isinstance(lease.session, MailSession)
                assert lease.session.login == "ada"
                assert lease.page.url == "https://mail.example/inbox"

    assert flow.logins == ["ada"]
    assert len(flow.prepared) == 1  # вкладка тёплая
    assert flow.closed == ["ada"]
    assert fake_driver.live == (0, 0, 0)  # временная вкладка входа тоже закрыта


async def test_state_is_saved_right_after_login(fake_driver: FakeDriver) -> None:
    store = MemoryStateStore()
    pool = make_pool(fake_driver, MailFlow(fake_driver), store=store)
    saved: list[StateSaved] = []
    pool.on(StateSaved, saved.append)

    async with pool, pool.page(A):
        record = await store.load("mail:a")
        assert record is not None
        assert record.state.cookies == (SID,)

    assert [event.trigger for event in saved] == ["open", "close"]


async def test_restart_after_login_does_not_log_in_again(fake_driver: FakeDriver) -> None:
    # Процесс упал сразу после входа: следующий запуск восстанавливает сессию из хранилища.
    store = MemoryStateStore()
    first = MailFlow(fake_driver)
    pool = make_pool(fake_driver, first, store=store)
    await pool.start()
    async with pool.page(A):
        pass
    await pool.terminate()

    second_driver = FakeDriver()
    second = MailFlow(second_driver)
    opened: list[SessionOpened] = []
    restarted = make_pool(second_driver, second, store=store)
    restarted.on(SessionOpened, opened.append)
    async with restarted, restarted.page(A) as lease:
        assert lease.context.state.cookies == (SID,)  # задано при создании контекста

    assert first.logins == ["ada"]
    assert second.logins == []
    assert [event.restored for event in opened] == [True]


async def test_initial_state_is_used_only_when_nothing_is_stored(fake_driver: FakeDriver) -> None:
    operator = SessionState(cookies=(SID,))
    flow = MailFlow(fake_driver)
    identity = Identity(key="mail:c", payload={"login": "cat"}, state=StatePolicy(initial=operator))

    async with make_pool(fake_driver, flow) as pool, pool.page(identity):
        pass

    assert flow.logins == []


async def test_read_only_state_is_never_saved(fake_driver: FakeDriver) -> None:
    store = MemoryStateStore()
    identity = Identity(key="mail:a", payload={"login": "ada"}, state=StatePolicy(mode="read_only"))

    async with (
        make_pool(fake_driver, MailFlow(fake_driver), store=store) as pool,
        pool.page(identity),
    ):
        pass

    assert await store.load("mail:a") is None


async def test_no_state_mode_does_not_touch_the_store(fake_driver: FakeDriver) -> None:
    store = MemoryStateStore()
    await store.save(IdentityRecord(key="mail:a", state=SessionState(cookies=(SID,))))
    flow = MailFlow(fake_driver)
    identity = Identity(key="mail:a", payload={"login": "ada"}, state=StatePolicy(mode="none"))

    async with make_pool(fake_driver, flow, store=store) as pool, pool.page(identity):
        pass

    assert flow.logins == ["ada"]  # сохранённое не читалось
    record = await store.load("mail:a")
    assert record is not None
    assert record.version == 1  # и не перезаписывалось


async def test_cookie_only_driver_gets_only_cookies() -> None:
    driver = FakeDriver(
        capabilities=DriverCapabilities(
            proxy_scope="context", can_new_context=True, state_support="cookies"
        )
    )
    operator = SessionState(
        cookies=(SID,),
        origins=(Origin(origin="https://mail.example", local_storage=(("t", "1"),)),),
    )
    identity = Identity(key="mail:a", payload={"login": "ada"}, state=StatePolicy(initial=operator))

    async with make_pool(driver, MailFlow(driver)) as pool, pool.page(identity) as lease:
        assert lease.context.state.cookies == (SID,)
        assert lease.context.state.origins == ()
    assert FAKE_CAPABILITIES.state_support == "full"


# --- сохранение ------------------------------------------------------------------------


async def test_state_is_saved_before_the_context_closes(fake_driver: FakeDriver) -> None:
    store = MemoryStateStore()
    fresher = Cookie(name="sid", value="rotated", domain=".mail.example")

    async with make_pool(fake_driver, MailFlow(fake_driver), store=store) as pool:
        async with pool.page(A) as lease:
            await fake_driver.add_cookies(lease.context, [fresher])
            await lease.retire_context("проверка")
        await asyncio.sleep(0)

        record = await store.load("mail:a")
        assert record is not None
        assert record.state.cookies == (fresher,)


async def test_save_before_close_continues_the_record_it_read(
    fake_driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    store = MemoryStateStore()
    saved: list[tuple[str, int]] = []

    async with make_pool(fake_driver, MailFlow(fake_driver), store=store) as pool:
        pool.on(StateSaved, lambda event: saved.append((event.trigger, event.version)))
        async with pool.page(A):
            pass

    assert saved == [("open", 1), ("close", 2)]
    assert "изменил кто-то ещё" not in caplog.text  # своя же запись — не конфликт


async def test_state_is_saved_periodically(fake_driver: FakeDriver) -> None:
    lifecycle = Lifecycle(healthcheck_interval=10.0, state_save_interval=60.0)
    recycling = Recycling(browser_max_leases=None, browser_max_age=None)
    pool = make_pool(fake_driver, MailFlow(fake_driver), lifecycle=lifecycle, recycling=recycling)
    triggers: list[str] = []
    pool.on(StateSaved, lambda event: triggers.append(event.trigger))

    async with pool:
        async with pool.page(A):
            pass
        await asyncio.sleep(75)

    assert triggers[:2] == ["open", "interval"]


async def test_dead_context_state_is_an_event_not_an_error(fake_driver: FakeDriver) -> None:
    store = MemoryStateStore()
    pool = make_pool(fake_driver, MailFlow(fake_driver), store=store)
    lost: list[StateExportFailed] = []
    pool.on(StateExportFailed, lost.append)

    async with pool:
        async with pool.page(A) as lease:
            browser = lease.browser
        fake_driver.crash(browser)
        await asyncio.sleep(1)

    assert [event.key for event in lost] == ["mail:a"]
    record = await store.load("mail:a")
    assert record is not None
    assert record.state.cookies == (SID,)  # сохранённое после входа не тронуто


async def test_export_state_reads_live_context_then_store(fake_driver: FakeDriver) -> None:
    store = MemoryStateStore()
    async with make_pool(fake_driver, MailFlow(fake_driver), store=store) as pool:
        async with pool.page(A):
            live = await pool.export_state("mail:a")
        assert live is not None
        assert live.cookies == (SID,)
        assert await pool.export_state("mail:nobody") is None


# --- сбои входа ------------------------------------------------------------------------


async def test_session_type_flows_to_a_typed_task(fake_driver: FakeDriver) -> None:
    async def task(lease: PageLease[FakeBrowser, FakeContext, FakePage, MailSession]) -> str:
        assert_type(lease.session, MailSession)  # pyright проверяет тип сессии из flow
        return lease.session.login

    async with make_pool(fake_driver, MailFlow(fake_driver)) as pool:
        assert await pool.run(task, A) == "ada"


async def test_failed_open_in_any_of_names_the_identity(fake_driver: FakeDriver) -> None:
    class SiteError(RuntimeError):
        pass

    class Broken(MailFlow):
        @override
        async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> MailSession:
            await ctx.new_page()
            msg = "капча"
            raise SiteError(msg)

    async with make_pool(fake_driver, Broken(fake_driver)) as pool:
        with pytest.raises(SiteError) as caught:
            async with pool.page(any_of=[A]):
                pass

        assert getattr(caught.value, "pool_identity", None) == "mail:a"
        assert any("mail:a" in note for note in caught.value.__notes__)


async def test_failed_login_closes_the_context_and_pauses_the_identity(
    fake_driver: FakeDriver,
) -> None:
    class Broken(MailFlow):
        @override
        async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> MailSession:
            await ctx.new_page()
            msg = "капча"
            raise RuntimeError(msg)

    async with make_pool(fake_driver, Broken(fake_driver)) as pool:
        with pytest.raises(RuntimeError, match="капча"):
            async with pool.page(A):
                pass

        assert fake_driver.live.contexts == 0
        assert fake_driver.live.pages == 0
        assert pool.identity_status("mail:a").open_failures == 1


async def test_login_that_declares_a_ban_blocks_the_identity(fake_driver: FakeDriver) -> None:
    class Banned(MailFlow):
        @override
        async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> MailSession:
            reason = "пароля нет"
            raise PoolSignal(reason, kind=ErrorKind.blocked)

    async with make_pool(fake_driver, Banned(fake_driver)) as pool:
        with pytest.raises(PoolSignal):
            async with pool.page(A):
                pass

        with pytest.raises(IdentityBlockedError):
            async with pool.page(A):
                pass


async def test_hanging_login_times_out(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, MailFlow(fake_driver, login_takes=1_000.0)) as pool:
        started = clock.monotonic()
        with pytest.raises(TimeoutError):
            async with pool.page(A):
                pass

        assert clock.monotonic() - started == pytest.approx(30.0)
        assert fake_driver.live.contexts == 0


async def test_logins_are_limited_in_parallel(fake_driver: FakeDriver) -> None:
    flow = MailFlow(fake_driver, login_takes=10.0)

    async with make_pool(fake_driver, flow, concurrent_opens=1) as pool:
        started = clock.monotonic()

        async def use(identity: Identity) -> float:
            async with pool.page(identity):
                return clock.monotonic() - started

        finished = await asyncio.gather(use(A), use(B))

    assert sorted(round(moment) for moment in finished) == [10, 20]


async def test_page_failing_reset_is_not_kept(fake_driver: FakeDriver) -> None:
    flow = MailFlow(fake_driver)
    async with make_pool(fake_driver, flow) as pool:
        flow.reset_answer = False
        async with pool.page(A) as lease:
            page = lease.page
        assert not page.alive


def test_base_flow_requires_open() -> None:
    class NoOpen(BaseFlow[object, object, object]):
        pass

    with pytest.raises(TypeError, match="open"):
        NoOpen()  # pyright: ignore[reportAbstractUsage] — проверяем отказ
