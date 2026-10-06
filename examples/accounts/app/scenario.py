"""Сценарий: N аккаунтов за прокси, раунды работы, падение браузера во втором раунде.

1. Стенд: сайт почты, который открывается только через прокси (имя `mail.demo` резолвят они).
2. Пул: 2 браузера, у каждого аккаунта свой контекст со своим прокси (sticky), сессии — в каталоге.
3. Раунды: каждый аккаунт проверяет свой ящик (`pool.run` с повтором). Первый вход — паролем.
4. Во втором раунде процесс одного браузера убивается посреди аренды. Упавшие задачи `pool.run`
   повторяет, пул перезапускает браузер и открывает контексты с сохранёнными сессиями.
5. Итог: работа сделана, браузер перезапущен, входов по паролю — ровно по одному на аккаунт.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from browser_pool import Backoff, BrowserPool, Identity, PoolConfig, ProxyPolicy
from browser_pool.config import Limits, Recovery, Recycling, Topology
from browser_pool.events import TaskRetried
from browser_pool.proxies import Proxy, ProxyList
from browser_pool.state import FileStateStore
from examples.accounts.app.drivers import DriverName, browser_sdk
from examples.accounts.app.errors import classify
from examples.accounts.app.flow import Account, MailFlow
from examples.accounts.harness import Stand, crash

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from browser_pool import PageLease
    from browser_pool.driver import Driver

type Pool = BrowserPool[Any, Any, Any]


@dataclass(slots=True)
class Report:
    """Итог сценария."""

    accounts: int
    rounds: int
    tasks_done: int = 0
    task_retries: int = 0
    killed_pid: int | None = None
    browser_restarts: int = 0
    password_logins: dict[str, int] = field(default_factory=dict[str, int])
    proxy_requests: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def passed(self) -> bool:
        """Работа сделана, браузер пережил убийство, повторных входов по паролю нет."""
        return (
            self.tasks_done == self.accounts * self.rounds
            and self.killed_pid is not None
            and self.browser_restarts >= 1
            and set(self.password_logins.values()) == {1}
            and len(self.password_logins) == self.accounts
        )


async def run(
    *,
    driver: DriverName = "playwright",
    accounts: int = 6,
    proxies: int = 3,
    rounds: int = 4,
    headless: bool = True,
    dump_after: float | None = None,
) -> Report:
    """Прогнать сценарий. `dump_after` — диагностика зависаний: стеки задач и снимок пула."""
    credentials = {f"user{index}": f"secret-{index}" for index in range(accounts)}
    report = Report(accounts=accounts, rounds=rounds)
    with Stand.up(credentials, proxies=proxies) as stand, tempfile.TemporaryDirectory() as state:
        app = make_app(stand, driver=driver, headless=headless, state_dir=Path(state))
        identities = [
            Identity(
                key=f"mail:{login}", payload=Account(login, password), proxy=ProxyPolicy.sticky()
            )
            for login, password in credentials.items()
        ]
        app.pool.on(TaskRetried, lambda _: setattr(report, "task_retries", report.task_retries + 1))
        async with app.pool, _dump_later(app.pool, dump_after):
            for round_number in range(rounds):
                await _round(app, identities, crash_first=round_number == 1, report=report)
            report.browser_restarts = app.pool.snapshot().counters.restarts
        report.password_logins = dict(stand.site.password_logins)
        report.proxy_requests = stand.proxy_requests()
    return report


@dataclass(frozen=True, slots=True)
class App:
    """Собранное приложение: пул и то, из чего он собран, — драйвер и flow нужны задачам."""

    pool: Pool
    driver: Driver[Any, Any, Any]
    flow: MailFlow


def make_app(stand: Stand, *, driver: DriverName, headless: bool, state_dir: Path) -> App:
    """Пул: драйвер и flow под выбранный SDK, прокси стенда, сессии в каталоге, классификатор."""
    native, client = browser_sdk(driver, base_url=stand.site.url(""), headless=headless)
    flow = MailFlow(client)
    pool: Pool = BrowserPool(
        native,
        config=PoolConfig(
            topology=Topology(browsers=2, pages_per_browser=3, contexts_per_browser=4),
            limits=Limits(spawn_delay=0.0, concurrent_opens=4),
            recycling=Recycling(browser_max_leases=None),
            recovery=Recovery(
                restart_backoff=Backoff(initial=0.5, maximum=60.0, factor=2.0, jitter=0.0)
            ),
        ),
        flow=flow,
        classifier=classify,
        state_store=FileStateStore(state_dir),
        proxy_source=ProxyList(
            [
                Proxy(host="127.0.0.1", port=port, id=f"proxy-{index}")
                for index, port in enumerate(stand.proxy_ports)
            ],
            strategy="sticky",
        ),
    )
    return App(pool=pool, driver=native, flow=flow)


async def _round(
    app: App, identities: list[Identity], *, crash_first: bool, report: Report
) -> None:
    """Каждый аккаунт проверяет ящик; `crash_first` — первый при этом роняет свой браузер."""

    async def check(lease: PageLease[Any, Any, Any, Any]) -> None:
        if crash_first and lease.identity is identities[0] and report.killed_pid is None:
            report.killed_pid = await crash(app.driver, lease.browser)
        await app.flow.client(lease.page).check_inbox(lease.identity.payload.login)

    results = await app.pool.map(
        check, identities, concurrency=len(identities), retries=4, backoff=Backoff.exp(0.5, 5)
    )
    report.tasks_done += len(results)


@contextlib.asynccontextmanager
async def _dump_later(pool: Pool, seconds: float | None) -> AsyncGenerator[None]:
    """Диагностика зависания: через `seconds` — стеки всех задач и снимок пула в stderr."""

    async def dump() -> None:
        await asyncio.sleep(seconds or 0)
        print(f"--- сценарий идёт дольше {seconds} с: задачи и снимок пула ---", file=sys.stderr)
        for task in asyncio.all_tasks():
            task.print_stack(file=sys.stderr)
        print(pool.snapshot(), file=sys.stderr, flush=True)

    watcher = asyncio.create_task(dump()) if seconds is not None else None
    try:
        yield
    finally:
        if watcher is not None:
            watcher.cancel()
