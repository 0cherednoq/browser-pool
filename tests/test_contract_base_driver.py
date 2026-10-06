"""Контрактный набор на самом маленьком драйвере — наследнике `BaseDriver` (как в `docs/extending/driver.md`).

Драйвер реализует только обязательное: запуск, живость, контекст и вкладку с их закрытием. «SDK» у
него — `urllib` тестового набора: браузер с прокси при запуске, один контекст на браузер, без
состояния, улик, окон и события обрыва. Набор при этом проходит: умолчания `BaseDriver` согласованы
с возможностями, которые драйвер не объявил.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import override

from browser_pool import ErrorKind, Proxy
from browser_pool.driver import BaseDriver, ContextSpec, Driver, DriverCapabilities, LaunchSpec
from browser_pool.testing.contract import DriverContractSuite
from browser_pool.testing.fake_http import FakeProxyError, fetch


@dataclass(eq=False, slots=True)
class TinyBrowser:
    proxy: Proxy | None
    closed: bool = False


@dataclass(eq=False, slots=True)
class TinyPage:
    browser: TinyBrowser
    closed: bool = field(default=False)


class TinyDriver(BaseDriver[TinyBrowser, TinyBrowser, TinyPage]):
    """Прокси задаётся при запуске, контекст — сам браузер."""

    @property
    @override
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(proxy_scope="browser", proxy_auth=True)

    @override
    async def launch(self, spec: LaunchSpec) -> TinyBrowser:
        return TinyBrowser(proxy=spec.proxy)

    @override
    async def ping(self, browser: TinyBrowser) -> bool:
        return not browser.closed

    @override
    async def close_browser(self, browser: TinyBrowser) -> None:
        browser.closed = True

    @override
    async def new_context(self, browser: TinyBrowser, spec: ContextSpec) -> TinyBrowser:
        return browser

    @override
    async def close_context(self, context: TinyBrowser) -> None:
        return None

    @override
    async def new_page(self, context: TinyBrowser) -> TinyPage:
        return TinyPage(browser=context)

    @override
    def page_usable(self, page: TinyPage) -> bool:
        return not page.closed and not page.browser.closed

    @override
    async def close_page(self, page: TinyPage) -> None:
        page.closed = True

    @override
    def classify(self, error: BaseException) -> ErrorKind | None:
        return ErrorKind.proxy if isinstance(error, FakeProxyError) else None


class TestBaseDriver(DriverContractSuite[TinyBrowser, TinyBrowser, TinyPage]):
    @override
    def make_driver(self) -> Driver[TinyBrowser, TinyBrowser, TinyPage]:
        return TinyDriver()

    @override
    async def visit(self, page: TinyPage, url: str) -> str:
        result = await asyncio.to_thread(fetch, url, proxy=page.browser.proxy, cookies=())
        return result.body
