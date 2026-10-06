"""Шаг 3: вход на сайт описан один раз, во flow, и выполняется один раз на аккаунт."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

from browser_pool import BaseFlow, BrowserPool, Identity, OpenRequest, PageLease
from browser_pool.drivers.playwright import PlaywrightDriver

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

type Lease = PageLease[Browser, BrowserContext, Page, str]


@dataclass(frozen=True, slots=True)
class Account:
    """Аккаунт сайта: его получает flow через `identity.payload`."""

    login: str
    password: str = field(repr=False)


class QuotesFlow(BaseFlow["BrowserContext", "Page", str]):
    """Как войти на quotes.toscrape.com. Пул зовёт `open` один раз на контекст аккаунта."""

    def __init__(self) -> None:
        self.logins = 0

    @override
    async def open(self, ctx: OpenRequest[BrowserContext, Page]) -> str:
        account: Account = ctx.identity.payload
        page = await ctx.new_page()
        await page.goto("https://quotes.toscrape.com/login")
        await page.fill("#username", account.login)
        await page.fill("#password", account.password)
        async with page.expect_navigation():
            await page.press("#password", "Enter")
        self.logins += 1
        return account.login


async def quotes_on_page(lease: Lease) -> str:
    """Задача аккаунта: вход уже выполнен, остаётся работа на странице."""
    await lease.page.goto("https://quotes.toscrape.com/")
    logged_in = await lease.page.locator("a[href='/logout']").count() == 1
    return f"{lease.session}: вход {'есть' if logged_in else 'потерян'}"


async def main() -> None:
    """Девять задач на три аккаунта: по три задачи на каждый."""
    accounts = [Account(login, "secret") for login in ("anna", "boris", "vera")]
    identities = [Identity(key=f"quotes:{account.login}", payload=account) for account in accounts]
    flow = QuotesFlow()

    async with BrowserPool(PlaywrightDriver(), flow=flow) as pool:
        results = await pool.map(quotes_on_page, identities * 3)

    for line in sorted(set(results)):
        print(line)
    print(f"задач: {len(results)}, входов по паролю: {flow.logins}")


if __name__ == "__main__":
    asyncio.run(main())
