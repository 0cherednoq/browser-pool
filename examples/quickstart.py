"""Быстрый старт: пул, identity, сессия, аренда, повтор — на фейковом драйвере, без браузера.

    uv run python -m examples.quickstart

Фейковый драйвер вместо браузера: flow и задача здесь просто пишут адрес вкладки вместо настоящей
навигации. С настоящим браузером драйвер — `PlaywrightDriver()` или `PydollDriver()`, а flow и
задачи зовут SDK сайта; как это устроено в проекте — `examples/quotes` и `examples/accounts`.
Пул, identity, аренда и повторы — те же.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import override

from browser_pool import (
    Backoff,
    BaseFlow,
    BrowserPool,
    Identity,
    OpenRequest,
    PageLease,
    PoolConfig,
    Topology,
)
from browser_pool.events import ContextOpened, LeaseReleased
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage

type Lease = PageLease[
    FakeBrowser, FakeContext, FakePage, str
]  # str — объект сессии, который вернул MailFlow.open


@dataclass(frozen=True, slots=True)
class Account:
    """Модель аккаунта приложения — payload identity. Пароль не попадёт в repr и логи пула."""

    login: str
    password: str = field(repr=False)


class MailFlow(BaseFlow[FakeContext, FakePage, str]):
    """Site SDK: как открыть сессию аккаунта. Пул зовёт `open` один раз на контекст."""

    @override
    async def open(self, ctx: OpenRequest[FakeContext, FakePage]) -> str:
        account: Account = ctx.identity.payload
        page = await ctx.new_page()
        page.url = f"https://mail.example/inbox?user={account.login}"  # «вход»
        return account.login


async def send(lease: Lease, text: str) -> str:
    """Операция site SDK на арендованной вкладке."""
    lease.page.url = f"https://mail.example/sent?by={lease.session}"
    return f"{lease.session}: {text}"


async def main() -> None:
    """Пул на два браузера, три аккаунта, аренда по одному и пачкой."""
    accounts = [Account(f"user{index}", f"secret-{index}") for index in range(3)]
    identities = [Identity(key=f"mail:{account.login}", payload=account) for account in accounts]
    pool = BrowserPool(
        FakeDriver(),
        config=PoolConfig.accounts().replace(topology=Topology(browsers=2, pages_per_browser=4)),
        flow=MailFlow(),
    )
    pool.on(ContextOpened, lambda event: print(f"контекст открыт: {event.key}"))
    pool.on(LeaseReleased, lambda event: print(f"аренда {event.lease_id} вернулась: {event.key}"))

    async with pool:
        # Одна аренда: вкладка identity в её контексте; сессия уже открыта flow.
        async with pool.page(identities[0]) as lease:
            print(await send(lease, "привет"))

        # Любой из кандидатов — сначала тот, у кого контекст уже открыт (вход дорог).
        async with pool.page(any_of=identities) as lease:
            print(f"досталась {lease.identity.key}")

        # Пачка с повтором: сбой вкладки/сессии/прокси — новая аренда после паузы.
        results = await pool.map(
            lambda lease: send(lease, "рассылка"),
            identities,
            concurrency=2,
            retries=2,
            backoff=Backoff.exp(1, 10),
        )
        print(results)

        snapshot = pool.snapshot()
        print(f"аренд сейчас {snapshot.leases_active}, контекстов {len(snapshot.contexts)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
