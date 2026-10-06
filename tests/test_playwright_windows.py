"""Окна для отладки на настоящем Chromium.

Автотесты идут в headless: окна там эмулируются, и CDP двигает их так же, как настоящие. Ручной
чек-лист (`@manual`, `pytest --manual`) открывает окна на экране — смотреть глазами.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING, Any

import pytest

from browser_pool import BrowserPool, Identity, PageLease, PoolConfig
from browser_pool.config import Limits, Recycling, Topology, Windows
from browser_pool.geometry import Rect

pytest.importorskip("playwright.async_api")

from browser_pool.drivers.playwright import PlaywrightDriver

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

pytestmark = pytest.mark.browser

type Pool = BrowserPool["Browser", "BrowserContext", "Page"]
type Lease = PageLease["Browser", "BrowserContext", "Page", Any]

ACCOUNTS = [Identity(key=f"mail:{index}") for index in range(6)]
SCREEN = Rect(x=0, y=0, width=1920, height=1040)


def overlap(first: Rect, second: Rect) -> bool:
    return not (
        first.x + first.width <= second.x
        or second.x + second.width <= first.x
        or first.y + first.height <= second.y
        or second.y + second.height <= first.y
    )


def windowed_pool(driver: PlaywrightDriver, windows: Windows) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=8, contexts_per_browser=8),
            limits=Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            windows=windows,
        ),
    )


async def bounds_of(driver: PlaywrightDriver, lease: Lease) -> Rect:
    window = await driver.window_of(lease.page)
    return (await driver.get_bounds(lease.browser, window)).rect


async def test_accounts_get_their_own_cells_on_real_chromium() -> None:
    driver = PlaywrightDriver(launch_options={"headless": True})
    rects: list[Rect] = []
    viewports: list[list[int]] = []

    async with windowed_pool(driver, Windows(mode="per_context", screen=SCREEN)) as pool:
        for identity in ACCOUNTS:
            async with pool.page(identity) as lease:
                rects.append(await bounds_of(driver, lease))
                viewports.append(await lease.page.evaluate("() => [innerWidth, innerHeight]"))
        snapshot = pool.windows.snapshot()

    assert len(snapshot) == 6
    assert not any(overlap(a, b) for a, b in itertools.combinations(rects, 2))
    assert all(rect.x + rect.width <= SCREEN.width for rect in rects)
    # fit_viewport: страница занимает окно, а не 1280×720 (в headless у окна нет рамки).
    assert viewports == [[rect.width, rect.height] for rect in rects]


async def test_idle_window_is_minimized_for_the_one_in_use_on_real_chromium() -> None:
    driver = PlaywrightDriver(launch_options={"headless": True})
    windows = Windows(mode="per_context", screen=SCREEN, max_windows=2)

    async with windowed_pool(driver, windows) as pool:
        for identity in ACCOUNTS[:3]:
            async with pool.page(identity):
                pass
        views = {view.key: view for view in pool.windows.snapshot()}

    assert views["mail:0"].folded  # самый давно простаивающий уступил ячейку
    assert not views["mail:1"].folded
    assert not views["mail:2"].folded


@pytest.mark.manual
async def test_headed_checklist() -> None:
    """Смотреть глазами: 6 окон сеткой без перекрытий; закрытие одного не двигает остальные."""
    driver = PlaywrightDriver(launch_options={"headless": False})

    async with windowed_pool(driver, Windows(mode="per_context")) as pool:
        for identity in ACCOUNTS:
            async with pool.page(identity) as lease:
                await lease.page.set_content(f"<h1>{identity.key}</h1>")
        await asyncio.sleep(5)  # 6 окон сеткой
        before = {view.key: view.rect for view in pool.windows.snapshot()}
        async with pool.page(ACCOUNTS[2]) as lease:
            await lease.retire_context("проверка")
        await asyncio.sleep(3)  # окно mail:2 закрылось, остальные на местах
        after = {view.key: view.rect for view in pool.windows.snapshot()}
        await pool.windows.retile()
        await asyncio.sleep(3)

    assert all(after[key] == before[key] for key in after)


@pytest.mark.manual
async def test_headed_window_per_tab_checklist() -> None:
    """Смотреть глазами: у каждого из 2 аккаунтов по 3 вкладки — 6 отдельных окон сеткой, не вкладки."""
    driver = PlaywrightDriver(launch_options={"headless": False})
    pool = windowed_pool(driver, Windows(mode="per_page"))

    async def hold(identity: Identity, release: asyncio.Event) -> None:
        async with pool.page(identity) as lease:
            await lease.page.set_content(f"<h1>{identity.key}</h1>")
            await release.wait()

    async with pool:
        release = asyncio.Event()
        identities = [identity for identity in ACCOUNTS[:2] for _ in range(3)]
        tasks = [asyncio.create_task(hold(identity, release)) for identity in identities]
        await asyncio.sleep(6)
        placed = pool.windows.snapshot()
        release.set()
        await asyncio.gather(*tasks)

    assert len(placed) == 6
