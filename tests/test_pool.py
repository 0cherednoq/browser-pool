"""Фасад пула и аренда: выдача, ожидание, отказы, отмена, остановка, вложения."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from browser_pool import (
    BrowserPool,
    ContextOptions,
    Identity,
    PageLease,
    PoolConfig,
    ProxyPolicy,
    clock,
)
from browser_pool.config import Limits, Timeouts, Topology
from browser_pool.errors import (
    AcquireTimeoutError,
    PoolSaturatedError,
    PoolStoppedError,
    StartupTimeoutError,
)
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

A = Identity(key="mail:a")
B = Identity(key="mail:b")

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


def make_pool(driver: FakeDriver, **topology: int) -> Pool:
    settings = {"browsers": 1, "pages_per_browser": 2, "warm_pages_per_identity": 1, **topology}
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(**settings),
            limits=Limits(max_waiting=3),
            timeouts=Timeouts(drain=30.0, close=2.0),
        ),
    )


async def hold(pool: Pool, identity: Identity, release: asyncio.Event) -> FakePage:
    async with pool.page(identity) as lease:
        await release.wait()
        return lease.page


# --- выдача ----------------------------------------------------------------------------


async def test_lease_gives_native_objects_and_returns_the_page_warm(
    fake_driver: FakeDriver,
) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            first = lease.page
            assert isinstance(lease.context, FakeContext)
            assert isinstance(lease.browser, FakeBrowser)
            assert lease.identity is A
            assert lease.page.alive

        async with pool.page(A) as lease:
            assert lease.page is first


async def test_any_of_prefers_an_open_context(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(B):
            pass

        async with pool.page(any_of=[A, B]) as lease:
            assert lease.identity.key == "mail:b"


@pytest.mark.parametrize(
    ("identity", "any_of"),
    [(None, None), (A, [B]), (None, [])],
    ids=["nothing", "both", "empty"],
)
async def test_page_needs_exactly_one_way_to_name_identities(
    fake_driver: FakeDriver, identity: Identity | None, any_of: Sequence[Identity] | None
) -> None:
    async with make_pool(fake_driver) as pool:
        with pytest.raises(ValueError, match="identity"):
            async with pool.page(identity, any_of=any_of):
                pass


async def test_identity_context_options_reach_the_driver(fake_driver: FakeDriver) -> None:
    identity = Identity(
        key="shop:1", context_options=ContextOptions(locale="de-DE", extra={"x": 1})
    )

    async with make_pool(fake_driver) as pool, pool.page(identity) as lease:
        assert lease.context.spec.locale == "de-DE"
        assert lease.context.spec.extra == {"x": 1}


# --- исключения арендатора -------------------------------------------------------------


async def test_tenant_error_propagates_unchanged_and_the_page_is_discarded(
    fake_driver: FakeDriver,
) -> None:
    error = RuntimeError("композер завис")

    pages: list[FakePage] = []

    async with make_pool(fake_driver) as pool:

        async def fail_inside() -> None:
            async with pool.page(A) as lease:
                pages.append(lease.page)
                raise error

        with pytest.raises(RuntimeError) as caught:
            await fail_inside()

        assert caught.value is error
        assert not pages[0].alive
        async with pool.page(A) as lease:
            assert lease.page is not pages[0]


async def test_page_can_be_discarded_without_an_error(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            lease.discard_page()
            discarded = lease.page

        assert not discarded.alive


async def test_physical_failure_releases_the_slot(fake_driver: FakeDriver) -> None:
    fake_driver.faults.fail("launch", RuntimeError("нет бинаря"))

    async with make_pool(fake_driver, pages_per_browser=1) as pool:
        with pytest.raises(RuntimeError, match="бинаря"):
            async with pool.page(A):
                pass

        async with pool.page(A) as lease:
            assert lease.page.alive


# --- ожидание --------------------------------------------------------------------------


async def test_waiter_gets_the_slot_when_it_is_released(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, pages_per_browser=1) as pool:
        release = asyncio.Event()
        holder = asyncio.create_task(hold(pool, A, release))
        await asyncio.sleep(0)
        acquired = asyncio.Event()

        async def second() -> None:
            async with pool.page(B):
                acquired.set()

        waiter = asyncio.create_task(second())
        await asyncio.sleep(1)
        assert not acquired.is_set()

        release.set()
        await holder
        await asyncio.wait_for(acquired.wait(), timeout=1)
        await waiter


async def test_timeout_raises_and_loses_no_slot(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, pages_per_browser=1) as pool:
        release = asyncio.Event()
        holder = asyncio.create_task(hold(pool, A, release))
        await asyncio.sleep(0)
        started = clock.monotonic()

        with pytest.raises(AcquireTimeoutError) as caught:
            async with pool.page(B, acquire_timeout=5):
                pass

        assert clock.monotonic() - started == pytest.approx(5)
        assert caught.value.candidates == ("mail:b",)
        release.set()
        await holder
        async with pool.page(B, acquire_timeout=5) as lease:
            assert lease.page.alive


async def test_cancelled_waiter_loses_no_slot(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, pages_per_browser=1) as pool:
        release = asyncio.Event()
        holder = asyncio.create_task(hold(pool, A, release))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(hold(pool, B, asyncio.Event()))
        await asyncio.sleep(0)

        # Отмена в тот же момент, когда слот освобождается: аренду выдадут, но забрать некому.
        release.set()
        waiter.cancel()
        await holder
        with pytest.raises(asyncio.CancelledError):
            await waiter

        async with pool.page(A, acquire_timeout=1) as lease:
            assert lease.page.alive


async def test_full_queue_rejects_at_once(fake_driver: FakeDriver) -> None:
    async with make_pool(fake_driver, pages_per_browser=1) as pool:
        release = asyncio.Event()
        tasks = [asyncio.create_task(hold(pool, A, release)) for _ in range(4)]
        await asyncio.sleep(0)

        with pytest.raises(PoolSaturatedError):
            async with pool.page(A):
                pass

        release.set()
        await asyncio.gather(*tasks)


# --- вывод контекста из работы ---------------------------------------------------------


async def test_retired_context_closes_after_the_lease_and_neighbours_live_on(
    fake_driver: FakeDriver,
) -> None:
    async with make_pool(fake_driver) as pool:
        release = asyncio.Event()
        neighbour = asyncio.create_task(hold(pool, B, release))
        await asyncio.sleep(0)

        async with pool.page(A) as lease:
            old_context, old_generation = lease.context, lease.generation
            await lease.retire_context("сессия протухла")
            assert not old_context.closed  # дорабатывает текущая аренда

        await asyncio.sleep(0)
        assert old_context.closed
        async with pool.page(A) as lease:
            assert lease.context is not old_context
            assert lease.generation > old_generation

        release.set()
        assert (await neighbour).alive is True


# --- вложения страницы -----------------------------------------------------------------


class Client:
    """Объект site SDK, который живёт с вкладкой."""

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_attachment_lives_with_the_page_across_leases(fake_driver: FakeDriver) -> None:
    created: list[Client] = []

    def factory(page: FakePage) -> Client:
        created.append(Client(page))
        return created[-1]

    async with make_pool(fake_driver) as pool:
        async with pool.page(A) as lease:
            first = await lease.attachment("sdk", factory)
        async with pool.page(A) as lease:
            again = await lease.attachment("sdk", factory)
            lease.discard_page()

        assert again is first
        assert len(created) == 1
        assert first.closed


async def test_async_factory_is_awaited(fake_driver: FakeDriver) -> None:
    async def factory(page: FakePage) -> Client:
        await asyncio.sleep(1)
        return Client(page)

    async with make_pool(fake_driver) as pool, pool.page(A) as lease:
        client = await lease.attachment("sdk", factory)
        assert client.page is lease.page


# --- остановка -------------------------------------------------------------------------


async def test_page_before_start_and_after_stop_is_refused(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    with pytest.raises(PoolStoppedError):
        async with pool.page(A):
            pass

    await pool.start()
    await pool.stop()

    with pytest.raises(PoolStoppedError):
        async with pool.page(A):
            pass


async def test_stop_drains_active_leases_and_refuses_waiters(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver, pages_per_browser=1)
    await pool.start()
    release = asyncio.Event()
    holder = asyncio.create_task(hold(pool, A, release))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(hold(pool, B, asyncio.Event()))
    await asyncio.sleep(0)

    stopping = asyncio.create_task(pool.stop())
    await asyncio.sleep(1)
    assert not stopping.done()  # ждёт занятую аренду
    with pytest.raises(PoolStoppedError):
        await waiter

    release.set()
    await holder
    await stopping
    assert fake_driver.live == (0, 0, 0)


async def test_stop_gives_up_on_leases_after_the_drain_timeout(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    await pool.start()
    release = asyncio.Event()
    holder = asyncio.create_task(hold(pool, A, release))
    await asyncio.sleep(0)
    started = clock.monotonic()

    await pool.stop()

    assert clock.monotonic() - started == pytest.approx(30)
    assert fake_driver.live == (0, 0, 0)
    release.set()
    await holder  # возврат после остановки не падает


async def test_terminate_does_not_wait(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    await pool.start()
    release = asyncio.Event()
    holder = asyncio.create_task(hold(pool, A, release))
    await asyncio.sleep(0)
    started = clock.monotonic()

    await pool.terminate()

    assert clock.monotonic() - started == pytest.approx(0)
    assert fake_driver.live == (0, 0, 0)
    release.set()
    await holder


async def test_driver_is_released_once_after_the_pool_closes(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    await pool.stop()  # не стартовал — освобождать нечего
    assert [call.operation for call in fake_driver.calls] == []

    async with make_pool(fake_driver) as started, started.page(A):
        pass
    await started.stop()  # повторная остановка

    operations = [call.operation for call in fake_driver.calls]
    assert operations.count("prepare") == operations.count("shutdown") == 1
    assert operations[-1] == "shutdown"


# --- запуск и мелочи -------------------------------------------------------------------


async def test_driver_that_never_gets_ready_is_a_pool_error(fake_driver: FakeDriver) -> None:
    fake_driver.faults.hang("prepare")
    pool = make_pool(fake_driver)

    with pytest.raises(StartupTimeoutError) as caught:
        await pool.start()

    assert isinstance(caught.value, TimeoutError)  # и по-старому ловится тоже
    assert caught.value.timeout == pool.config.timeouts.startup
    await pool.stop()


async def test_sticky_proxy_without_a_source_is_called_out_once(
    fake_driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    sticky = Identity(key="mail:sticky", proxy=ProxyPolicy.sticky())

    async with make_pool(fake_driver) as pool:
        for _ in range(2):
            async with pool.page(sticky) as lease:
                assert lease.proxy is None
        async with pool.page(A):  # обычная identity без источника — напрямую, это не новость
            pass

    warnings = [record for record in caplog.records if "sticky" in record.getMessage()]
    assert len(warnings) == 1
    assert "mail:sticky" in warnings[0].getMessage()
    assert "proxy_source" in warnings[0].getMessage()


async def test_before_release_hook_still_owns_the_lease(fake_driver: FakeDriver) -> None:
    pool = make_pool(fake_driver)
    seen: list[object] = []

    @pool.hooks.before_release
    async def tidy(lease: PageLease[FakeBrowser, FakeContext, FakePage, object]) -> None:
        seen.append(await lease.attachment("client", lambda page: f"client-{page.id}"))
        seen.append(await lease.cookies())

    async with pool:
        async with pool.page(A) as lease:
            first = lease.page
        async with pool.page(A) as lease:
            assert lease.page is first  # хук не упал — вкладка вернулась тёплой

    assert seen == [f"client-{first.id}", (), f"client-{first.id}", ()]
