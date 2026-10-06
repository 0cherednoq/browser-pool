"""Окна для отладки (M8.18, §4 ревью): ячейка после выброшенной вкладки, `size` больше экрана, `free`, headless."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Limits, Recycling, Topology, Windows
from browser_pool.driver import WindowBounds, WindowState
from browser_pool.geometry import Rect
from browser_pool.testing import FAKE_CAPABILITIES, FakeBrowser, FakeContext, FakeDriver, FakePage
from browser_pool.windows.manager import WindowManager

WINDOWED = dataclasses.replace(FAKE_CAPABILITIES, window_control="runtime", new_window=True)
ACCOUNTS = [Identity(key=f"mail:{index}") for index in range(8)]

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


# --- §4 п. 1: ячейка после выброшенной вкладки -------------------------------------------------


async def test_new_tab_after_the_only_tab_was_discarded_gets_the_accounts_cell() -> None:
    driver = windowed_driver()

    async with make_pool(driver) as pool:
        async with pool.page(ACCOUNTS[0]) as first:
            cell = first.page.window.bounds.rect
            first.discard_page()
        assert not first.page.alive

        again = await use(pool, ACCOUNTS[0])  # новая вкладка — новое окно браузера

        assert again.window.bounds.rect == cell
        (view,) = pool.windows.snapshot()
        assert view.rect == cell
        assert view.slot == 0


# --- §4 п. 3: арендованное окно не сворачивается; free ------------------------------------------


async def test_size_larger_than_the_screen_still_gives_a_leased_window_a_cell() -> None:
    driver = windowed_driver()

    async with make_pool(driver, size=(2600, 1500)) as pool, pool.page(ACCOUNTS[0]) as lease:
        bounds = lease.page.window.bounds
        assert bounds.state is WindowState.normal
        assert bounds.rect.width <= driver.screen.width
        assert bounds.rect.height <= driver.screen.height


async def test_a_window_in_use_is_never_minimized_for_lack_of_cells() -> None:
    driver = windowed_driver()

    async with (
        make_pool(driver, size=(1900, 1000), overflow="minimize_idle") as pool,
        pool.page(ACCOUNTS[0]) as first,
        pool.page(ACCOUNTS[1]) as second,
    ):
        assert first.page.window.bounds.state is WindowState.normal
        assert second.page.window.bounds.state is WindowState.normal


async def test_free_layout_resizes_every_window_and_minimizes_none() -> None:
    driver = windowed_driver()

    async with make_pool(driver, layout="free", size=(900, 700)) as pool:
        pages = [await use(pool, identity) for identity in ACCOUNTS[:6]]

        for page in pages:
            assert page.window.bounds.state is WindowState.normal
            assert (page.window.bounds.rect.width, page.window.bounds.rect.height) == (900, 700)


# --- §4 п. 4: счётчик active при сбое размещения ------------------------------------------------


async def test_failed_first_placement_does_not_leave_the_window_busy() -> None:
    driver = windowed_driver()
    driver.faults.fail("set_bounds", RuntimeError("ОС отказала"), times=1)

    async with make_pool(driver) as pool:
        await use(pool, ACCOUNTS[0])
        (view,) = pool.windows.snapshot()

    assert view.active == 0


# --- §4 п. 5: headless ---------------------------------------------------------------------------


class HeadlessDriver(FakeDriver):
    """Драйвер, которому владелец велел headless: окон нет, двигать нечего."""

    headless = True


async def test_headless_driver_turns_the_windows_section_off_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = HeadlessDriver(capabilities=WINDOWED)

    with caplog.at_level(logging.WARNING):
        async with make_pool(driver) as pool:
            await use(pool, ACCOUNTS[0])
            assert pool.windows.snapshot() == ()

    assert "headless" in caplog.text
    (browser,) = driver.browsers
    assert browser.spec is not None
    assert browser.spec.headless is True


async def test_no_display_turns_the_windows_section_off_and_launches_headless(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("browser_pool.windows.manager.display_available", lambda: False)
    driver = windowed_driver()

    with caplog.at_level(logging.WARNING):
        async with make_pool(driver) as pool:
            await use(pool, ACCOUNTS[0])
            assert pool.windows.snapshot() == ()

    assert "экран" in caplog.text
    (browser,) = driver.browsers
    assert browser.spec is not None
    assert browser.spec.headless is True


# --- §4 п. 6: screen="auto" ----------------------------------------------------------------------


async def test_auto_screen_grows_when_a_later_tab_sees_a_bigger_monitor() -> None:
    driver = windowed_driver()
    driver.screen = Rect(x=0, y=0, width=1280, height=720)  # первая вкладка с эмуляцией viewport

    async with make_pool(driver, reflow="fill") as pool:
        first = await use(pool, ACCOUNTS[0])
        small = first.window.bounds.rect
        driver.screen = Rect(x=0, y=0, width=2560, height=1400)
        await use(pool, ACCOUNTS[1])

        assert first.window.bounds.rect.height > small.height  # окна встали в большую область


# --- §4 п. 7: min_size и ручной сдвиг --------------------------------------------------------------


def test_default_min_size_is_not_smaller_than_the_chrome_minimum_window() -> None:
    assert (
        Windows().min_size[0] >= 534
    )  # Chrome не делает окно у́же — окна наползли бы друг на друга


async def test_small_drift_of_the_window_is_not_a_manual_move() -> None:
    driver = windowed_driver()

    async with make_pool(driver, reflow="fill") as pool:
        first = await use(pool, ACCOUNTS[0])
        placed = first.window.bounds.rect
        # Chrome/ОС поправили границы на пару пикселей (тень окна, масштаб).
        first.window.bounds = WindowBounds(
            rect=Rect(x=placed.x + 3, y=placed.y + 2, width=placed.width - 4, height=placed.height)
        )
        await use(pool, ACCOUNTS[1])  # сетка пересчитывается

        (view, _) = pool.windows.snapshot()
        assert not view.manual
        assert first.window.bounds.rect.width != placed.width - 4  # окно вернули в ячейку


async def test_real_manual_move_is_still_respected() -> None:
    driver = windowed_driver()

    async with make_pool(driver, reflow="fill") as pool:
        first = await use(pool, ACCOUNTS[0])
        moved = Rect(x=300, y=300, width=700, height=500)
        first.window.bounds = WindowBounds(rect=moved)
        await use(pool, ACCOUNTS[1])

        assert first.window.bounds.rect == moved
        assert pool.windows.snapshot()[0].manual


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("browser_pool.drivers.playwright", "PlaywrightDriver"),
        ("browser_pool.drivers.pydoll", "PydollDriver"),
    ],
)
def test_headless_flag_of_a_shipped_driver_switches_the_windows_section_off(
    module: str, name: str
) -> None:
    driver_class = getattr(pytest.importorskip(module), name)
    config = Windows(mode="per_context")

    def manager(driver: object) -> WindowManager[Any, Any]:
        return WindowManager[Any, Any](
            driver,  # pyright: ignore[reportArgumentType]
            config=config,
            locate=lambda key: None,
            emit=lambda event: None,
            timeout=1.0,
        )

    headless = manager(driver_class(headless=True))
    undecided = manager(driver_class())

    assert not headless.shown
    assert "headless" in (headless.problem() or "")
    assert undecided.shown  # None — решает пул
