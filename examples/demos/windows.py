"""Демо для ролика «окна друг на друге»: шесть аккаунтов без пула и с пулом.

    uv run --extra playwright python -m examples.demos.windows --mode before   # шесть окон в одной точке
    uv run --extra playwright python -m examples.demos.windows --mode after    # окна сеткой, с подписями

Сеть не нужна: страница аккаунта задаётся прямо в коде. Запись экрана — `scripts/record_demo.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from browser_pool import BrowserPool, Identity, PageLease, PoolConfig, ProxyPolicy
from browser_pool.config import Debug, Topology, Windows

PAGE = """
<title>Почта</title>
<body style="font: 28px sans-serif; display: grid; place-items: center; height: 90vh; margin: 0">
  <div>Ящик аккаунта <b>{key}</b></div>
</body>
"""


async def before(keys: list[str], hold: float) -> None:
    """Без пула: каждый аккаунт запускает свой браузер, окна ложатся одно на другое."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:

        async def work(key: str) -> None:
            browser = await playwright.chromium.launch(headless=False)
            try:
                page = await browser.new_page()
                await page.set_content(PAGE.format(key=key))
                await asyncio.sleep(hold)
            finally:
                await browser.close()

        await asyncio.gather(*(work(key) for key in keys))


async def after(keys: list[str], hold: float) -> None:
    """С пулом: окно на аккаунт, окна сеткой, в заголовке — чьё окно."""
    from browser_pool.drivers.playwright import PlaywrightDriver  # noqa: PLC0415 — SDK может не стоять

    config = PoolConfig(
        topology=Topology(browsers=2, pages_per_browser=8),
        windows=Windows(mode="per_context"),
        debug=Debug(label_windows=True),
    )
    accounts = [Identity(key=key, proxy=ProxyPolicy.direct()) for key in keys]

    async def work(lease: PageLease[Any, Any, Any, Any]) -> None:
        await lease.page.set_content(PAGE.format(key=lease.identity.key))
        await asyncio.sleep(hold)

    async with BrowserPool(PlaywrightDriver(), config=config) as pool:
        await pool.map(work, accounts, concurrency=len(accounts))


def main() -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description="окна шести аккаунтов: без пула и с пулом")
    parser.add_argument("--mode", choices=["before", "after"], required=True)
    parser.add_argument("--accounts", type=int, default=6)
    parser.add_argument("--hold", type=float, default=6.0, help="сколько секунд держать окна")
    options = parser.parse_args()
    keys = [f"acc:{number}" for number in range(1, options.accounts + 1)]
    scenario = before if options.mode == "before" else after
    asyncio.run(scenario(keys, options.hold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
