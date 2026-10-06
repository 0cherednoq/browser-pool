"""Контрактный набор драйвера на Playwright Chromium."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import pytest

from browser_pool.driver import Driver
from browser_pool.testing.contract import DriverContractSuite

pytest.importorskip("playwright.async_api")

from browser_pool.drivers.playwright import PlaywrightDriver

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

pytestmark = pytest.mark.browser


class TestPlaywrightChromium(DriverContractSuite["Browser", "BrowserContext", "Page"]):
    @override
    def make_driver(self) -> Driver[Browser, BrowserContext, Page]:
        return PlaywrightDriver()

    @override
    async def visit(self, page: Page, url: str) -> str:
        await page.goto(url, timeout=15_000)
        return await page.inner_text("body")

    @override
    async def evaluate(self, page: Page, expression: str) -> object:
        return await page.evaluate(expression)
