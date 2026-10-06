"""Демо для ролика «гонки при входе»: пять задач одного аккаунта без пула и с пулом.

    uv run --extra playwright python -m examples.demos.login --mode before   # пять входов по паролю
    uv run --extra playwright python -m examples.demos.login --mode after    # один вход, остальные ждут

Сайт почты и прокси перед ним поднимаются локально (стенд примера `accounts`). В конце печатается,
сколько раз сайт принял пароль: без пула — по разу на задачу, с пулом — один раз.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import sys
from typing import Any

from browser_pool import BrowserPool, Identity, PageLease, PoolConfig, ProxyPolicy
from browser_pool.config import Debug, Topology, Windows
from browser_pool.proxies import Proxy, ProxyList
from examples.accounts.app.flow import Account, MailFlow
from examples.accounts.harness import Stand
from examples.accounts.mail_sdk.playwright import PlaywrightMailClient

LOGIN = "anna"
PASSWORD = "secret-anna"  # noqa: S105 — аккаунт локального стенда
SLOW_MO = 150
"""Замедление операций, мс: чтобы за входом можно было уследить."""


async def before(stand: Stand, *, tasks: int, hold: float, headless: bool) -> None:
    """Без пула: каждая задача видит «не залогинен» и входит сама."""
    from playwright.async_api import async_playwright

    server = f"http://127.0.0.1:{stand.proxy_ports[0]}"

    async with async_playwright() as playwright:

        async def work() -> None:
            browser = await playwright.chromium.launch(
                headless=headless, slow_mo=SLOW_MO, proxy={"server": server}
            )
            try:
                client = PlaywrightMailClient(await browser.new_page(), base_url=stand.site.url(""))
                if not await client.is_logged_in():
                    await client.login(LOGIN, PASSWORD)
                await client.check_inbox(LOGIN)
                await asyncio.sleep(hold)
            finally:
                await browser.close()

        await asyncio.gather(*(work() for _ in range(tasks)))


async def after(stand: Stand, *, tasks: int, hold: float, headless: bool) -> None:
    """С пулом: вход один, остальные задачи ждут его и получают залогиненный контекст."""
    from browser_pool.drivers.playwright import PlaywrightDriver  # noqa: PLC0415 — SDK может не стоять

    client = functools.partial(PlaywrightMailClient, base_url=stand.site.url(""))
    config = PoolConfig(
        topology=Topology(browsers=1, pages_per_browser=tasks, pages_per_identity=tasks),
        windows=Windows(mode="per_page"),
        debug=Debug(label_windows=True, slow_mo=SLOW_MO),
    )
    account = Identity(
        key=f"mail:{LOGIN}", payload=Account(LOGIN, PASSWORD), proxy=ProxyPolicy.sticky()
    )
    proxies = ProxyList([Proxy(host="127.0.0.1", port=stand.proxy_ports[0], id="proxy-0")])

    async def work(lease: PageLease[Any, Any, Any, Any]) -> None:
        await client(lease.page).check_inbox(LOGIN)
        await asyncio.sleep(hold)

    driver = PlaywrightDriver(headless=True if headless else None)
    async with BrowserPool(
        driver, config=config, flow=MailFlow(client), proxy_source=proxies
    ) as pool:
        await asyncio.gather(*(pool.run(work, account) for _ in range(tasks)))


def main() -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description="пять задач одного аккаунта: без пула и с пулом")
    parser.add_argument("--mode", choices=["before", "after"], required=True)
    parser.add_argument("--tasks", type=int, default=5)
    parser.add_argument("--hold", type=float, default=4.0, help="сколько секунд держать окна")
    parser.add_argument("--headless", action="store_true", help="без окон: только счёт входов")
    options = parser.parse_args()
    scenario = before if options.mode == "before" else after
    with Stand.up({LOGIN: PASSWORD}, proxies=1) as stand:
        asyncio.run(
            scenario(stand, tasks=options.tasks, hold=options.hold, headless=options.headless)
        )
        logins = stand.site.password_logins[LOGIN]
    print(f"задач: {options.tasks}, входов по паролю: {logins}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
