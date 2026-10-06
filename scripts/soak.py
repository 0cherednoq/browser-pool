"""Длинный прогон пула на настоящем браузере: плановые перезапуски, простой, утечки.

    uv run --all-extras python scripts/soak.py --driver pydoll --minutes 30

Локальный сайт (`ContractSite`) и N аккаунтов. Аккаунт входит один раз (`/login`), дальше
каждая задача проверяет, что вкладка всё ещё его (`/whoami`): сессия должна переживать плановые
перезапуски браузеров и закрытие простаивающих контекстов — её восстанавливает пул из хранилища.
Работа идёт волнами по две минуты; после каждой второй — пауза, в которой срабатывают
`context_idle_ttl`, `page_idle_ttl`, `browser_idle_ttl`.

Раз в минуту — строка: аренды, ошибки, перезапуски, память процессов браузеров. В конце пул
останавливается, и скрипт проверяет, что после него не осталось дочерних процессов. Код выхода 0 —
ошибок задач нет, чужих сессий нет, плановые перезапуски были, процессов не осталось.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, cast

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.clock import monotonic, utc_now
from browser_pool.config import Lifecycle, Limits, Recycling, Topology
from browser_pool.events import BrowserRecycled, BrowserRestarted, ContextClosed
from browser_pool.state import MemoryStateStore
from browser_pool.testing.contract_site import ContractSite

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from browser_pool.driver import Driver
    from browser_pool.lease import PageLease

type Visit = Callable[[object, str], Awaitable[str]]
type Pool = BrowserPool[Any, Any, Any]

WAVE = 120.0
"""Длина волны работы, секунды."""
PAUSE = 90.0
"""Пауза после каждой второй волны: дольше всех TTL простоя в конфиге ниже."""


@dataclass(slots=True)
class Report:
    """Итог прогона."""

    minutes: float
    leases: int = 0
    wrong_session: int = 0
    errors: Counter[str] = field(default_factory=Counter[str])
    recycled: int = 0
    restarted: int = 0
    contexts_closed: int = 0
    peak_rss_mb: float = 0.0
    last_rss_mb: float = 0.0
    leftover_processes: list[str] = field(default_factory=list[str])

    @property
    def passed(self) -> bool:
        """Ошибок нет, сессии свои, плановые перезапуски были, процессов после пула нет."""
        return (
            not self.errors
            and not self.wrong_session
            and self.recycled > 0
            and not self.leftover_processes
        )


def driver_and_visit(name: str) -> tuple[Driver[Any, Any, Any], Visit]:
    """Драйвер и «site SDK» в одну функцию: открыть адрес и вернуть текст страницы."""
    if name == "pydoll":
        from browser_pool.drivers.pydoll import PydollDriver  # noqa: PLC0415 — SDK может не стоять

        async def visit_pydoll(page: object, url: str) -> str:
            tab = cast("Any", page)
            await tab.go_to(url, timeout=20)
            response = await tab.execute_script("document.body.innerText", return_by_value=True)
            return str(cast("dict[str, Any]", response)["result"]["result"]["value"]).strip()

        return PydollDriver(), visit_pydoll

    from browser_pool.drivers.playwright import PlaywrightDriver  # noqa: PLC0415 — SDK может не стоять

    async def visit_playwright(page: object, url: str) -> str:
        native = cast("Any", page)
        await native.goto(url, timeout=20_000)
        return str(await native.inner_text("body")).strip()

    return PlaywrightDriver(), visit_playwright


def make_pool(driver: Driver[Any, Any, Any], report: Report) -> Pool:
    """Пул с короткими сроками: за полчаса — десятки перезапусков и закрытий по простою."""
    pool: Pool = BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=2, pages_per_browser=4, contexts_per_browser=4),
            limits=Limits(spawn_delay=0.2),
            lifecycle=Lifecycle(
                browser_idle_ttl=60.0,
                context_idle_ttl=45.0,
                page_idle_ttl=20.0,
                state_save_interval=30.0,
                healthcheck_interval=5.0,
            ),
            recycling=Recycling(browser_max_leases=150, browser_max_age=240.0),
        ),
        state_store=MemoryStateStore(),
    )
    pool.on(BrowserRecycled, lambda _: _bump(report, "recycled"))
    pool.on(BrowserRestarted, lambda _: _bump(report, "restarted"))
    pool.on(ContextClosed, lambda _: _bump(report, "contexts_closed"))
    return pool


async def soak(driver_name: str, *, minutes: float, accounts: int) -> Report:
    """Прогнать пул `minutes` минут и вернуть итог."""
    driver, visit = driver_and_visit(driver_name)
    report = Report(minutes=minutes)
    identities = [Identity(key=f"user{index}") for index in range(accounts)]
    pool = make_pool(driver, report)
    with ContractSite() as site:

        async def task(lease: PageLease[Any, Any, Any, Any]) -> None:
            name = lease.identity.key
            if await visit(lease.page, site.url("/whoami")) == name:
                return
            await visit(lease.page, site.url(f"/login?user={name}"))
            if await visit(lease.page, site.url("/whoami")) != name:
                report.wrong_session += 1

        async with pool:
            await _waves(pool, task, identities=identities, report=report, minutes=minutes)
        await asyncio.sleep(3)  # процессы, которым послан kill, успевают выйти
        report.leftover_processes = [f"{pid} {name}" for pid, name in _children()]
    return report


async def _waves(
    pool: Pool,
    task: Callable[[PageLease[Any, Any, Any, Any]], Awaitable[None]],
    *,
    identities: list[Identity],
    report: Report,
    minutes: float,
) -> None:
    """Волны работы до срока; после каждой второй — пауза для закрытия по простою."""
    deadline = monotonic() + minutes * 60
    wave = 0
    while monotonic() < deadline:
        wave += 1
        until = min(deadline, monotonic() + WAVE)
        await _wave(pool, task, identities=identities, report=report, until=until)
        if wave % 2 == 0 and monotonic() < deadline:
            await asyncio.sleep(min(PAUSE, deadline - monotonic()))
            _line(pool, report, note="после паузы")


async def _wave(
    pool: Pool,
    task: Callable[[PageLease[Any, Any, Any, Any]], Awaitable[None]],
    *,
    identities: list[Identity],
    report: Report,
    until: float,
) -> None:
    """Работа без пауз до `until`; раз в минуту — строка состояния."""
    next_line = monotonic()
    while monotonic() < until:
        results = await pool.map(
            task, identities * 3, concurrency=6, retries=2, return_exceptions=True
        )
        report.leases += len(results)
        report.errors.update(
            type(result).__name__ for result in results if isinstance(result, Exception)
        )
        if monotonic() >= next_line:
            _line(pool, report)
            next_line = monotonic() + 60


def _bump(report: Report, name: str) -> None:
    setattr(report, name, getattr(report, name) + 1)


def _line(pool: Pool, report: Report, *, note: str = "") -> None:
    snapshot = pool.snapshot()
    rss = _children_rss() / 2**20
    report.last_rss_mb = round(rss, 1)
    report.peak_rss_mb = max(report.peak_rss_mb, report.last_rss_mb)
    print(
        f"{utc_now():%H:%M:%S} аренд {report.leases} ошибок {sum(report.errors.values())}"
        f" перезапусков {report.recycled}/{report.restarted}"
        f" браузеров {len(snapshot.browsers)} контекстов {len(snapshot.contexts)}"
        f" RSS {rss:.0f} МБ {note}",
        flush=True,
    )


def _children() -> list[tuple[int, str]]:
    import psutil

    found: list[tuple[int, str]] = []
    for child in psutil.Process().children(recursive=True):
        try:
            found.append((child.pid, child.name()))
        except psutil.Error:
            continue
    return found


def _children_rss() -> int:
    import psutil

    total = 0
    for child in psutil.Process().children(recursive=True):
        try:
            total += child.memory_info().rss
        except psutil.Error:
            continue
    return total


def main() -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description="Длинный прогон пула на настоящем браузере")
    parser.add_argument("--driver", choices=["playwright", "pydoll"], default="playwright")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--accounts", type=int, default=6)
    options = parser.parse_args()
    report = asyncio.run(soak(options.driver, minutes=options.minutes, accounts=options.accounts))
    print(json.dumps({**asdict(report), "passed": report.passed}, ensure_ascii=False))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
