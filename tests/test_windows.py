"""Окна для отладки: сетка без перекрытий, стабильные ячейки, ручной сдвиг, нехватка места, сбои."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Limits, Recycling, Topology, Windows
from browser_pool.driver import WindowState
from browser_pool.events import WindowPlaced
from browser_pool.geometry import Rect
from browser_pool.testing import FAKE_CAPABILITIES, FakeBrowser, FakeContext, FakeDriver, FakePage

WINDOWED = dataclasses.replace(FAKE_CAPABILITIES, window_control="runtime", new_window=True)
SCREEN = Rect(x=0, y=0, width=1920, height=1040)
ACCOUNTS = [Identity(key=f"mail:{index}") for index in range(6)]

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]


def windowed_driver() -> FakeDriver:
    return FakeDriver(capabilities=WINDOWED)


def make_pool(driver: FakeDriver, **windows: object) -> Pool:
    settings: dict[str, object] = {"mode": "per_context", "gap": 8, **windows}
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=8, contexts_per_browser=8),
            limits=Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            windows=Windows(**settings),  # pyright: ignore[reportArgumentType] — поля секции
        ),
    )


async def use(pool: Pool, identity: Identity) -> FakePage:
    async with pool.page(identity) as lease:
        return lease.page


def overlap(first: Rect, second: Rect) -> bool:
    return not (
        first.x + first.width <= second.x
        or second.x + second.width <= first.x
        or first.y + first.height <= second.y
        or second.y + second.height <= first.y
    )


# --- раскладка -------------------------------------------------------------------------


async def test_accounts_get_side_by_side_windows() -> None:
    driver = windowed_driver()
    placed: list[WindowPlaced] = []

    async with make_pool(driver) as pool:
        pool.on(WindowPlaced, placed.append)
        pages = [await use(pool, identity) for identity in ACCOUNTS]
        await asyncio.sleep(0)
        rects = [page.window.bounds.rect for page in pages]
        views = pool.windows.snapshot()

    assert len(set(rects)) == 6
    assert not any(overlap(a, b) for a, b in itertools.combinations(rects, 2))
    assert all(rect.x >= 0 and rect.x + rect.width <= SCREEN.width for rect in rects)
    assert {event.key for event in placed} == {identity.key for identity in ACCOUNTS}
    assert sorted(view.slot or 0 for view in views) == list(range(6))


async def test_closing_one_account_does_not_move_the_others() -> None:
    driver = windowed_driver()

    async with make_pool(driver, max_windows=6) as pool:
        pages = [await use(pool, identity) for identity in ACCOUNTS[:4]]
        before = [page.window.bounds.rect for page in pages]
        async with pool.page(ACCOUNTS[1]) as lease:
            await lease.retire_context("проверка")
        await asyncio.sleep(0)
        newcomer = await use(pool, ACCOUNTS[4])

        after = [page.window.bounds.rect for page in (pages[0], pages[2], pages[3])]
        assert after == [before[0], before[2], before[3]]
        assert newcomer.window.bounds.rect == before[1]  # освободившаяся ячейка


async def test_window_outlives_the_tab_it_was_found_by() -> None:
    """Якорная вкладка закрылась (discard) — окно аккаунта держит вторая, ячейка та же."""
    driver = windowed_driver()

    async with make_pool(driver, max_windows=6) as pool:
        async with pool.page(ACCOUNTS[0]) as first, pool.page(ACCOUNTS[0]) as second:
            cell = first.page.window.bounds.rect
            first.discard_page()
        assert not first.page.alive
        await use(pool, ACCOUNTS[1])  # новая вкладка — менеджер сверяет, чьи вкладки живы

        views = {view.identity: view for view in pool.windows.snapshot()}
        assert views["mail:0"].slot == 0
        assert views["mail:0"].rect == cell
        assert views["mail:1"].slot == 1
        assert second.page.window.bounds.rect == cell

        again = await use(pool, ACCOUNTS[0])  # аккаунт остаётся в своей ячейке
        assert again.window.bounds.rect == cell
        assert len(pool.windows.snapshot()) == 2


async def test_window_moved_by_hand_stays_until_retile() -> None:
    driver = windowed_driver()
    elsewhere = Rect(x=100, y=100, width=700, height=500)

    async with make_pool(driver, reflow="fill") as pool:
        first = await use(pool, ACCOUNTS[0])
        driver.move_window(first, elsewhere)
        await use(pool, ACCOUNTS[1])  # сетка пересчитывается под два окна

        assert first.window.bounds.rect == elsewhere
        assert pool.windows.snapshot()[0].manual

        await pool.windows.retile()
        assert first.window.bounds.rect != elsewhere
        assert not pool.windows.snapshot()[0].manual


# --- нехватка места ----------------------------------------------------------------------


async def test_idle_window_gives_its_cell_to_the_one_in_use() -> None:
    driver = windowed_driver()

    async with make_pool(driver, max_windows=2) as pool:
        first = await use(pool, ACCOUNTS[0])
        second = await use(pool, ACCOUNTS[1])
        cell = first.window.bounds.rect

        third = await use(pool, ACCOUNTS[2])  # ячеек две — самое давно простаивающее уступает
        assert first.window.bounds.state is WindowState.minimized
        assert third.window.bounds.rect == cell
        assert second.window.bounds.state is WindowState.normal

        again = await use(pool, ACCOUNTS[0])  # понадобилось снова — разворачивается
        assert again is first
        assert first.window.bounds.state is WindowState.normal
        assert second.window.bounds.state is WindowState.minimized


async def test_minimize_idle_and_focus() -> None:
    driver = windowed_driver()

    async with make_pool(driver) as pool:
        first = await use(pool, ACCOUNTS[0])
        await use(pool, ACCOUNTS[1])

        assert await pool.windows.minimize_idle() == 2
        assert first.window.bounds.state is WindowState.minimized

        assert await pool.windows.focus("mail:0")
        assert first.window.bounds.state is WindowState.normal
        assert first.window.fronted == 1
        assert not await pool.windows.focus("mail:nobody")


async def test_window_per_page_gives_each_tab_its_own_window() -> None:
    driver = windowed_driver()

    async with make_pool(driver, mode="per_page") as pool:
        async with pool.page(ACCOUNTS[0]) as one, pool.page(ACCOUNTS[0]) as two:
            assert one.page is not two.page
            assert not overlap(one.page.window.bounds.rect, two.page.window.bounds.rect)
        assert len(pool.windows.snapshot()) == 2


# --- сбои и выключение -----------------------------------------------------------------


async def test_failing_window_operation_never_breaks_a_lease(
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = windowed_driver()
    driver.faults.fail("set_bounds", RuntimeError("ОС отказала"), times=3)

    async with make_pool(driver) as pool, pool.page(ACCOUNTS[0]) as lease:
        assert lease.page.alive

    assert "Окна:" in caplog.text


async def test_driver_without_window_control_is_warned_about_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = FakeDriver()  # window_control="none"

    with caplog.at_level(logging.WARNING):
        async with make_pool(driver) as pool:
            await use(pool, ACCOUNTS[0])
            await use(pool, ACCOUNTS[1])
            assert pool.windows.snapshot() == ()

    assert caplog.text.count("Окна для отладки выключены") == 1


async def test_windows_make_the_browser_headed() -> None:
    driver = windowed_driver()

    async with make_pool(driver) as pool:
        await use(pool, ACCOUNTS[0])

    (browser,) = driver.browsers
    assert browser.spec is not None
    assert browser.spec.headless is False


async def test_launch_only_driver_gets_the_cell_before_launch() -> None:
    capabilities = dataclasses.replace(FAKE_CAPABILITIES, window_control="launch_only")
    driver = FakeDriver(capabilities=capabilities)
    pool = BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=2, pages_per_browser=1),
            limits=Limits(spawn_delay=0.0),
            windows=Windows(mode="per_context", screen=SCREEN),
        ),
    )

    async with pool, pool.page(ACCOUNTS[0]), pool.page(ACCOUNTS[1]):
        windows = [browser.spec.window for browser in driver.browsers if browser.spec]
        assert len(windows) == 2
        assert all(window is not None for window in windows)
        first, second = windows
        assert first is not None
        assert second is not None
        assert not overlap(first, second)


async def test_second_tab_of_an_account_joins_its_cell() -> None:
    driver = windowed_driver()

    async with make_pool(driver) as pool:
        async with pool.page(ACCOUNTS[0]) as one, pool.page(ACCOUNTS[0]) as two:
            # У фейка, как у Playwright, каждая вкладка — своё окно: второе встаёт в ячейку аккаунта.
            assert one.page.window is not two.page.window
            assert two.page.window.bounds.rect == one.page.window.bounds.rect
        assert len(pool.windows.snapshot()) == 1


async def test_per_page_without_new_window_warns_and_works_per_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = FakeDriver(
        capabilities=dataclasses.replace(FAKE_CAPABILITIES, window_control="runtime")
    )
    release = asyncio.Event()

    async def hold() -> None:
        async with pool.page(ACCOUNTS[0]):
            await release.wait()

    with caplog.at_level(logging.WARNING):
        async with make_pool(driver, mode="per_page", screen=SCREEN) as pool:
            tasks = [asyncio.create_task(hold()) for _ in range(2)]
            await asyncio.sleep(0.1)
            views = pool.windows.snapshot()
            release.set()
            await asyncio.gather(*tasks)

    assert [view.key for view in views] == ["mail:0"]  # две вкладки — одно окно аккаунта
    assert caplog.text.count("per_page' недоступен") == 1
    assert "выключены" not in caplog.text


# --- окна аккаунта рядом (group="identity") ------------------------------------------------

TALL = Rect(x=0, y=0, width=1920, height=1240)
"""3×3 ячейки при min_size 560×400."""


def grouped_pool(driver: FakeDriver, screen: Rect) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(
                browsers=1, pages_per_browser=12, contexts_per_browser=8, pages_per_identity=3
            ),
            limits=Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            windows=Windows(mode="per_page", group="identity", gap=8, screen=screen),
        ),
    )


async def hold_tabs(pool: Pool, identities: list[Identity], release: asyncio.Event) -> None:
    """Вкладки аккаунтов открываются вперемешку: a, b, c, a, b, c, …"""

    async def hold(identity: Identity) -> None:
        async with pool.page(identity):
            await release.wait()

    tasks: list[asyncio.Task[None]] = []
    for identity in identities:
        tasks.append(asyncio.create_task(hold(identity)))
        await asyncio.sleep(0.01)
    await asyncio.gather(*tasks)


async def test_tabs_of_an_account_stay_together_in_its_row() -> None:
    driver = windowed_driver()
    accounts = ACCOUNTS[:3]
    release = asyncio.Event()

    async with grouped_pool(driver, TALL) as pool:
        tasks = asyncio.create_task(hold_tabs(pool, accounts * 3, release))
        await asyncio.sleep(1.0)
        views = pool.windows.snapshot()
        release.set()
        await tasks
        await asyncio.sleep(0.1)

    assert len(views) == 9
    for account in accounts:
        mine = sorted(
            view.slot for view in views if view.identity == account.key and view.slot is not None
        )
        assert len(mine) == 3
        assert mine == list(range(mine[0], mine[0] + 3))  # подряд
        assert mine[0] % 3 == 0  # с начала строки
        rows = {view.rect.y for view in views if view.identity == account.key and view.rect}
        assert len(rows) == 1  # одна строка — один аккаунт


async def test_account_without_a_free_block_is_folded_not_mixed_in() -> None:
    driver = windowed_driver()
    accounts = ACCOUNTS[:3]  # на экране 2 блока по 3 ячейки
    release = asyncio.Event()

    async with grouped_pool(driver, SCREEN) as pool:
        tasks = asyncio.create_task(hold_tabs(pool, accounts * 3, release))
        await asyncio.sleep(1.0)
        views = pool.windows.snapshot()
        release.set()
        await tasks
        await asyncio.sleep(0.1)

    placed = [view for view in views if view.slot is not None]
    assert len(placed) == 6
    for account in {view.identity for view in placed}:
        mine = sorted(
            view.slot for view in placed if view.identity == account and view.slot is not None
        )
        assert mine == list(range(mine[0], mine[0] + 3))
    assert len({view.identity for view in placed}) == 2  # третий ждёт свободный блок
