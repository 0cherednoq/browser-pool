"""Приложение: аккаунты читают цитаты в несколько вкладок; два запуска пула — сессии переживают рестарт.

    uv run --extra playwright python -m examples.quotes.app                    # без окон
    uv run --extra playwright python -m examples.quotes.app --mode windows     # окно на аккаунт
    uv run --extra playwright python -m examples.quotes.app --sessions sdk     # сессии хранит SDK

1. Запуск 1: пул поднимается, каждый аккаунт входит через форму (flow → SDK), сессия сохраняется —
   в хранилище пула или в хранилище SDK (`--sessions`). У аккаунта `--parallel` вкладок читают его
   страницы одновременно (`pool.run` с повтором).
2. Запуск 2: новый пул, как новый процесс. Сессии восстанавливаются, пароль не вводится ни разу.
3. Итог: все страницы прочитаны, входов по паролю — по одному на аккаунт, ошибок нет. Код выхода 0.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from examples.quotes.app.flow import QuotesFlow
from examples.quotes.app.pool import MODES, Mode, Pool, Sessions, identities, make_pool
from examples.quotes.quotes_sdk import QuotesClient, QuotesPage, SessionVault

if TYPE_CHECKING:
    from browser_pool import Identity, PageLease


@dataclass(slots=True)
class Report:
    """Итог: сколько прочитано, сколько раз вводили пароль, сколько вкладок работало разом."""

    expected: int
    parallel: int
    pages: list[QuotesPage] = field(default_factory=list[QuotesPage])
    errors: list[str] = field(default_factory=list[str])
    password_logins: dict[str, int] = field(default_factory=dict[str, int])
    active: dict[str, int] = field(default_factory=dict[str, int])
    peak: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def passed(self) -> bool:
        """Всё прочитано, по одному входу на аккаунт, вкладки аккаунта работали параллельно."""
        return (
            not self.errors
            and len(self.pages) == self.expected
            and set(self.password_logins.values()) == {1}
            and set(self.peak.values()) == {self.parallel}
        )


async def read_pages(
    pool: Pool, accounts: list[Identity], *, pages: int, dwell: float, report: Report
) -> None:
    """Каждый аккаунт читает `pages` страниц; параллельно — сколько вкладок разрешает пул."""

    async def job(identity: Identity, number: int) -> None:
        async def read(lease: PageLease[Any, Any, Any, Any]) -> QuotesPage:
            key = identity.key
            report.active[key] = report.active.get(key, 0) + 1
            report.peak[key] = max(report.peak.get(key, 0), report.active[key])
            try:
                page = await QuotesClient(lease.page).read_page(number)
                await asyncio.sleep(dwell)  # вкладки аккаунта работают вместе
            finally:
                report.active[key] -= 1
            return page

        try:
            result = await pool.run(read, identity, retries=2, acquire_timeout=180)
        except Exception as error:  # noqa: BLE001 — приложение собирает ошибки в итог
            report.errors.append(f"{identity.key} стр. {number}: {type(error).__name__}: {error}")
            return
        report.pages.append(result)
        say(
            f"{identity.key} стр. {number}: {len(result.quotes)} цитат, первая — {result.quotes[0].author}"
        )

    await asyncio.gather(
        *(
            job(identity, 1 + (index * pages + offset) % 10)
            for index, identity in enumerate(accounts)
            for offset in range(pages)
        )
    )


async def main_async(
    *, mode: Mode, sessions: Sessions, accounts: int, parallel: int, pages: int, hold: float
) -> Report:
    """Два запуска пула на одних и тех же сохранённых сессиях."""
    report = Report(expected=2 * accounts * pages, parallel=parallel)
    with tempfile.TemporaryDirectory(prefix="quotes-") as root:
        vault = SessionVault(Path(root) / "vault") if sessions == "sdk" else None
        state_dir = Path(root) / "pool" if sessions == "pool" else None
        flow = QuotesFlow(vault=vault)
        people = identities(accounts, sessions=sessions)
        for run in (1, 2):
            say(f"=== запуск {run}: пул поднимается ({mode}, сессии у {sessions}) ===")
            pool = make_pool(
                flow, mode=mode, state_dir=state_dir, accounts=accounts, parallel=parallel
            )
            await one_run(pool, people, mode=mode, pages=pages, hold=hold, report=report)
            say(f"=== запуск {run}: пул остановлен ===")
    report.password_logins = dict(flow.password_logins)
    return report


async def one_run(
    pool: Pool, people: list[Identity], *, mode: Mode, pages: int, hold: float, report: Report
) -> None:
    """Один запуск пула: работа всех аккаунтов; в отладочном режиме — окна напоказ."""
    async with pool:
        dwell = 0.5 if mode == "headless" else 1.5
        await read_pages(pool, people, pages=pages, dwell=dwell, report=report)
        if hold and mode != "headless":
            say(f"окна открыты ещё {hold:g} с — посмотрите")
            await asyncio.sleep(hold)


_START = monotonic()


def say(text: str) -> None:
    """Строка журнала со временем от старта."""
    print(f"[{monotonic() - _START:6.1f}s] {text}", flush=True)


def main() -> int:
    """Точка входа: печатает итог, код выхода 0 — всё работает."""
    parser = argparse.ArgumentParser(description="browser-pool + quotes_sdk: два слоя")
    parser.add_argument("--mode", choices=MODES, default="headless")
    parser.add_argument("--sessions", choices=["pool", "sdk"], default="pool")
    parser.add_argument("--accounts", type=int, default=3)
    parser.add_argument("--parallel", type=int, default=3, help="вкладок на аккаунт одновременно")
    parser.add_argument("--pages", type=int, default=6, help="страниц на аккаунт за запуск")
    parser.add_argument("--hold", type=float, default=6.0, help="сколько секунд держать окна")
    options = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    report = asyncio.run(
        main_async(
            mode=options.mode,
            sessions=options.sessions,
            accounts=options.accounts,
            parallel=options.parallel,
            pages=options.pages,
            hold=options.hold,
        )
    )
    print(f"\nстраниц прочитано: {len(report.pages)} из {report.expected}")
    print(f"входов по паролю: {report.password_logins} (во втором запуске — ни одного)")
    print(f"вкладок аккаунта одновременно (пик): {report.peak}")
    print(f"ошибок: {len(report.errors)}")
    for error in report.errors:
        print(f"  {error}")
    print(f"ИТОГ: {'ВСЁ РАБОТАЕТ' if report.passed else 'ЕСТЬ ПРОБЛЕМЫ'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
