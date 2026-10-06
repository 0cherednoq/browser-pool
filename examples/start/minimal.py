"""Шаг 1: пул, одна identity, одна вкладка."""

from __future__ import annotations

import asyncio

from browser_pool import BrowserPool, Identity
from browser_pool.drivers.playwright import PlaywrightDriver


async def main() -> None:
    """Открыть страницу и напечатать её заголовок."""
    async with BrowserPool(PlaywrightDriver()) as pool:
        async with pool.page(Identity(key="demo")) as lease:
            await lease.page.goto("https://quotes.toscrape.com/")
            print(await lease.page.title())


if __name__ == "__main__":
    asyncio.run(main())
