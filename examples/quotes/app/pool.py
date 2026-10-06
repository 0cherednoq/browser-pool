"""Сборка пула: драйвер, конфиг, flow, классификатор, хранилище сессий, окна для отладки."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from browser_pool import BrowserPool, Identity, PoolConfig, ProxyPolicy, StatePolicy
from browser_pool.config import Debug, Limits, Recycling, Topology, Windows
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.state import FileStateStore, MemoryStateStore
from examples.quotes.app.errors import classify
from examples.quotes.app.flow import Account

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.async_api import Browser, BrowserContext, Page

    from examples.quotes.app.flow import QuotesFlow

type Mode = Literal["headless", "windows", "tabs", "tabs-grouped"]
type Sessions = Literal["pool", "sdk"]
type Pool = BrowserPool["Browser", "BrowserContext", "Page"]

MODES: tuple[Mode, ...] = ("headless", "windows", "tabs", "tabs-grouped")
"""`headless` — без окон, как в проде; `windows` — окно на аккаунт; `tabs` — окно на вкладку;
`tabs-grouped` — окно на вкладку, окна аккаунта рядом (аккаунт — строка сетки)."""


def identities(count: int, *, sessions: Sessions) -> list[Identity]:
    """Аккаунты приложения как identity пула. Сессией владеет пул или SDK — одно из двух."""
    state = StatePolicy() if sessions == "pool" else StatePolicy(mode="none")
    return [
        Identity(
            key=f"quotes:user{index}",
            payload=Account(f"user{index}", f"secret-{index}"),
            proxy=ProxyPolicy.direct(),
            state=state,
            labels={"service": "quotes"},
        )
        for index in range(1, count + 1)
    ]


def make_pool(
    flow: QuotesFlow, *, mode: Mode, state_dir: Path | None, accounts: int, parallel: int
) -> Pool:
    """Пул под режим окон. `state_dir` — где пул хранит сессии; `None` — сессии у SDK.

    Вкладок на браузер — столько, чтобы все аккаунты со всеми вкладками поместились в один:
    как пул распределит контексты по двум браузерам, заранее не известно.
    """
    headless = mode == "headless"
    return BrowserPool(
        PlaywrightDriver(),
        config=PoolConfig(
            topology=Topology(
                browsers=2,
                pages_per_browser=max(8, accounts * parallel),
                contexts_per_browser=max(8, accounts),
                pages_per_identity=parallel,
                warm_pages_per_identity=parallel,
            ),
            limits=Limits(spawn_delay=0.2, concurrent_opens=4),
            recycling=Recycling(browser_max_leases=None),
            windows=Windows()
            if headless
            else Windows(
                mode="per_context" if mode == "windows" else "per_page",
                group="identity" if mode == "tabs-grouped" else "none",
                gap=6,
            ),
            debug=Debug() if headless else Debug(label_windows=True, keep_background_active=True),
        ),
        flow=flow,
        classifier=classify,
        state_store=FileStateStore(state_dir) if state_dir is not None else MemoryStateStore(),
    )
