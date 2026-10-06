"""Шаг 2: три identity работают одновременно в одном браузере."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from browser_pool import BrowserPool, Identity, PageLease
from browser_pool.drivers.playwright import PlaywrightDriver

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

type Lease = PageLease[Browser, BrowserContext, Page, Any]


async def first_author(lease: Lease) -> str:
    """Автор первой цитаты по тегу, который записан в payload identity."""
    tag: str = lease.identity.payload
    await lease.page.goto(f"https://quotes.toscrape.com/tag/{tag}/")
    author = await lease.page.locator(".quote .author").first.inner_text()
    return f"{lease.identity.key}: {author}"


async def main() -> None:
    """Раздать задачу трём identity и посмотреть, сколько браузеров на это ушло."""
    readers = [Identity(key=f"reader:{tag}", payload=tag) for tag in ("love", "humor", "books")]

    async with BrowserPool(PlaywrightDriver()) as pool:
        for line in await pool.map(first_author, readers):
            print(line)

        snapshot = pool.snapshot()
        launched = [browser for browser in snapshot.browsers if browser.launched]
        print(f"браузеров запущено: {len(launched)}, контекстов: {len(snapshot.contexts)}")


if __name__ == "__main__":
    asyncio.run(main())
