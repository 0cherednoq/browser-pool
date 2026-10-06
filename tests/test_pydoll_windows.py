"""Окна для отладки на pydoll и системном Chrome: `per_context` — окно на аккаунт.

Автотесты идут в headless: окна там эмулируются, и CDP двигает их так же, как настоящие. Ручной
чек-лист (`@manual`, `pytest --manual`) открывает окна на экране — смотреть глазами.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING, Any, cast

import pytest

from browser_pool import BrowserPool, Identity, PageLease, PoolConfig
from browser_pool.config import Debug, Limits, Recycling, Topology, Windows
from browser_pool.geometry import Rect

pytest.importorskip("pydoll")

from browser_pool.drivers.pydoll import PydollContext, PydollDriver

if TYPE_CHECKING:
    from pydoll.browser.chromium.base import Browser
    from pydoll.browser.tab import Tab

pytestmark = [
    pytest.mark.browser,
    pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction':DeprecationWarning"),
]

type Pool = BrowserPool["Browser", PydollContext, "Tab"]
type Lease = PageLease["Browser", PydollContext, "Tab", Any]

ACCOUNTS = [Identity(key=f"mail:{index}") for index in range(6)]
SCREEN = Rect(x=0, y=0, width=1920, height=1040)


def overlap(first: Rect, second: Rect) -> bool:
    return not (
        first.x + first.width <= second.x
        or second.x + second.width <= first.x
        or first.y + first.height <= second.y
        or second.y + second.height <= first.y
    )


def windowed_pool(driver: PydollDriver, windows: Windows, debug: Debug | None = None) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=8, contexts_per_browser=8),
            limits=Limits(spawn_delay=0.0),
            recycling=Recycling(browser_max_leases=None, browser_max_age=None),
            windows=windows,
            debug=debug or Debug(),
        ),
    )


async def evaluate(tab: Tab, script: str) -> Any:
    response = cast("dict[str, Any]", await tab.execute_script(script, return_by_value=True))
    return response["result"]["result"].get("value")


async def bounds_of(driver: PydollDriver, lease: Lease) -> Rect:
    window = await driver.window_of(lease.page)
    return (await driver.get_bounds(lease.browser, window)).rect


async def test_accounts_get_their_own_cells() -> None:
    driver = PydollDriver(
        arguments=("--headless=new",)
    )  # автотесту окон headless нужен без выключения секции
    rects: list[Rect] = []
    windows: set[int] = set()

    async with windowed_pool(driver, Windows(mode="per_context", screen=SCREEN)) as pool:
        for identity in ACCOUNTS:
            async with pool.page(identity) as lease:
                rects.append(await bounds_of(driver, lease))
                windows.add(int(await driver.window_of(lease.page)))
        snapshot = pool.windows.snapshot()

    assert len(snapshot) == 6
    assert len(windows) == 6  # у каждого контекста своё окно
    assert not any(overlap(a, b) for a, b in itertools.combinations(rects, 2))
    assert all(rect.x + rect.width <= SCREEN.width for rect in rects)


async def test_tabs_of_one_account_share_its_window() -> None:
    driver = PydollDriver(
        arguments=("--headless=new",)
    )  # автотесту окон headless нужен без выключения секции
    release, both = asyncio.Event(), asyncio.Event()
    seen: list[int] = []

    async def hold(pool: Pool) -> None:
        async with pool.page(ACCOUNTS[0]) as lease:
            seen.append(int(await driver.window_of(lease.page)))
            if len(seen) == 2:
                both.set()
            await release.wait()

    async with windowed_pool(driver, Windows(mode="per_context", screen=SCREEN)) as pool:
        tasks = [asyncio.create_task(hold(pool)) for _ in range(2)]
        await asyncio.wait_for(both.wait(), timeout=30)
        release.set()
        await asyncio.gather(*tasks)
        views = pool.windows.snapshot()

    assert seen[0] == seen[1]
    assert [view.key for view in views] == ["mail:0"]


@pytest.mark.manual
async def test_headed_checklist() -> None:
    """Смотреть глазами: 6 окон Chrome сеткой без перекрытий, в заголовках — ключ аккаунта;
    закрытие одного не двигает остальные; `retile()` возвращает всё в сетку."""
    driver = PydollDriver(headless=False)
    pool = windowed_pool(driver, Windows(mode="per_context"), Debug(label_windows=True))

    async with pool:
        for identity in ACCOUNTS:
            async with pool.page(identity) as lease:
                await evaluate(lease.page, f"document.body.innerHTML = '<h1>{identity.key}</h1>'")
        await asyncio.sleep(5)  # 6 окон сеткой
        before = {view.key: view.rect for view in pool.windows.snapshot()}
        async with pool.page(ACCOUNTS[2]) as lease:
            await lease.retire_context("проверка")
        await asyncio.sleep(3)  # окно mail:2 закрылось, остальные на местах
        after = {view.key: view.rect for view in pool.windows.snapshot()}
        await pool.windows.retile()
        await asyncio.sleep(3)

    assert all(after[key] == before[key] for key in after)
