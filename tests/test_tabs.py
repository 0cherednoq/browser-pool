"""Тёплые вкладки контекста: LIFO, прогрев один раз, выброс сломанных, старение."""

from __future__ import annotations

import asyncio
from typing import override

import pytest

from browser_pool import clock
from browser_pool._core.tabs import TabPool
from browser_pool.config import Timeouts
from browser_pool.driver import ContextSpec, LaunchSpec
from browser_pool.testing import FakeContext, FakeDriver, FakePage


class Flow:
    """Прогрев и сброс вкладки глазами site SDK — с журналом."""

    def __init__(self) -> None:
        self.prepared: list[int] = []
        self.reset_answer: bool | Exception = True

    async def prepare(self, page: FakePage) -> None:
        page.url = "https://mail.example/inbox"
        self.prepared.append(page.id)

    async def reset(self, page: FakePage) -> bool:
        _ = page
        if isinstance(self.reset_answer, Exception):
            raise self.reset_answer
        return self.reset_answer


async def make_tabs(
    driver: FakeDriver, *, warm: int = 2, flow: Flow | None = None
) -> tuple[TabPool[FakeContext, FakePage], Flow]:
    browser = await driver.launch(LaunchSpec())
    context = await driver.new_context(browser, ContextSpec())
    flow = flow or Flow()
    tabs = TabPool(
        driver,
        context,
        prepare=flow.prepare,
        reset=flow.reset,
        warm_limit=warm,
        timeouts=Timeouts(page_create=5.0, prepare_page=10.0, reset_page=3.0, close=2.0),
    )
    return tabs, flow


async def close_all(driver: FakeDriver) -> None:
    for browser in driver.browsers:
        await driver.close_browser(browser)


async def test_new_page_is_prepared_once_and_reused_warm(fake_driver: FakeDriver) -> None:
    tabs, flow = await make_tabs(fake_driver)

    page = await tabs.take()
    await tabs.give_back(page)
    again = await tabs.take()

    assert again is page
    assert flow.prepared == [page.id]
    assert (tabs.open, tabs.idle) == (1, 0)
    await tabs.give_back(again)
    await close_all(fake_driver)


async def test_most_recently_returned_page_is_taken_first(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver)
    first, second = await tabs.take(), await tabs.take()

    await tabs.give_back(first)
    await tabs.give_back(second)

    assert await tabs.take() is second
    await close_all(fake_driver)


async def test_warm_limit_closes_the_coldest(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver, warm=1)
    pages = [await tabs.take() for _ in range(3)]

    for page in pages:
        await tabs.give_back(page)

    assert (tabs.open, tabs.idle) == (1, 1)
    assert [fake_driver.page_usable(page) for page in pages] == [False, False, True]
    await close_all(fake_driver)


async def test_zero_warm_limit_closes_every_returned_page(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver, warm=0)
    page = await tabs.take()

    await tabs.give_back(page)

    assert (tabs.open, tabs.idle) == (0, 0)
    assert not fake_driver.page_usable(page)
    await close_all(fake_driver)


@pytest.mark.parametrize("answer", [False, RuntimeError("композер завис")], ids=["false", "raises"])
async def test_page_failing_reset_is_discarded(
    fake_driver: FakeDriver, answer: bool | Exception
) -> None:
    tabs, flow = await make_tabs(fake_driver)
    page = await tabs.take()
    flow.reset_answer = answer

    await tabs.give_back(page)

    assert (tabs.open, tabs.idle) == (0, 0)
    assert not fake_driver.page_usable(page)
    await close_all(fake_driver)


async def test_dead_warm_page_is_skipped(fake_driver: FakeDriver) -> None:
    tabs, flow = await make_tabs(fake_driver)
    page = await tabs.take()
    await tabs.give_back(page)
    page.closed = True  # вкладку закрыл сам сайт, пока она лежала тёплой

    fresh = await tabs.take()

    assert fresh is not page
    assert flow.prepared == [page.id, fresh.id]
    assert tabs.open == 1
    await close_all(fake_driver)


async def test_page_given_back_dead_is_not_kept(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver)
    page = await tabs.take()
    page.closed = True

    await tabs.give_back(page)

    assert (tabs.open, tabs.idle) == (0, 0)
    await close_all(fake_driver)


async def test_failed_prepare_closes_the_page_and_propagates(fake_driver: FakeDriver) -> None:
    class Failing(Flow):
        @override
        async def prepare(self, page: FakePage) -> None:
            _ = page
            msg = "SPA не загрузилась"
            raise RuntimeError(msg)

    tabs, _ = await make_tabs(fake_driver, flow=Failing())

    with pytest.raises(RuntimeError, match="SPA"):
        await tabs.take()

    assert tabs.open == 0
    assert fake_driver.live.pages == 0
    await close_all(fake_driver)


async def test_hanging_prepare_times_out(fake_driver: FakeDriver) -> None:
    class Hanging(Flow):
        @override
        async def prepare(self, page: FakePage) -> None:
            _ = page
            await asyncio.get_running_loop().create_future()

    tabs, _ = await make_tabs(fake_driver, flow=Hanging())
    started = clock.monotonic()

    with pytest.raises(TimeoutError):
        await tabs.take()

    assert clock.monotonic() - started == pytest.approx(10)
    assert tabs.open == 0
    assert fake_driver.live.pages == 0
    await close_all(fake_driver)


async def test_hanging_page_creation_times_out(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver)
    fake_driver.faults.hang("new_page")

    with pytest.raises(TimeoutError):
        await tabs.take()

    assert tabs.open == 0
    await close_all(fake_driver)


async def test_hanging_close_neither_blocks_nor_raises(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver, warm=0)
    page = await tabs.take()
    fake_driver.faults.hang("close_page")
    started = clock.monotonic()

    await tabs.give_back(page)

    assert clock.monotonic() - started == pytest.approx(2)
    assert tabs.close_failures == 1
    assert tabs.open == 0  # вкладка больше не наша: учёт не должен её держать
    await close_all(fake_driver)


async def test_idle_pages_expire(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver, warm=3)
    old, young = await tabs.take(), await tabs.take()
    await tabs.give_back(old)
    await asyncio.sleep(200)
    await tabs.give_back(young)
    await asyncio.sleep(150)

    closed = await tabs.expire_idle(idle_ttl=300.0)

    assert closed == 1
    assert not fake_driver.page_usable(old)
    assert fake_driver.page_usable(young)
    await close_all(fake_driver)


async def test_trim_closes_the_coldest_idle_page(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver)
    cold, warm = await tabs.take(), await tabs.take()
    await tabs.give_back(cold)
    await tabs.give_back(warm)

    assert await tabs.trim_idle()
    assert not fake_driver.page_usable(cold)
    assert await tabs.trim_idle()
    assert not await tabs.trim_idle()
    await close_all(fake_driver)


async def test_aclose_closes_idle_pages(fake_driver: FakeDriver) -> None:
    tabs, _ = await make_tabs(fake_driver)
    page = await tabs.take()
    await tabs.give_back(page)

    await tabs.aclose()

    assert (tabs.open, tabs.idle) == (0, 0)
    assert fake_driver.live.pages == 0
    await close_all(fake_driver)


async def test_discard_is_safe_while_handling_an_error(fake_driver: FakeDriver) -> None:
    # Закрытие вкладки после ошибки арендатора не должно заслонять эту ошибку.
    tabs, _ = await make_tabs(fake_driver)
    page = await tabs.take()
    fake_driver.faults.fail("close_page", RuntimeError("закрытие упало"))

    await tabs.discard(page)

    assert tabs.open == 0
    assert tabs.close_failures == 1
    await close_all(fake_driver)
