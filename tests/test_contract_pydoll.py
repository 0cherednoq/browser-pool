"""Контрактный набор драйвера на pydoll и системном Chrome."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import pytest

from browser_pool.driver import Driver
from browser_pool.testing.contract import DriverContractSuite

pytest.importorskip("pydoll")

from browser_pool.drivers.pydoll import PydollContext, PydollDriver

if TYPE_CHECKING:
    from pydoll.browser.chromium.base import Browser
    from pydoll.browser.tab import Tab

pytestmark = [
    pytest.mark.browser,
    # pydoll 2.27 зовёт устаревший asyncio.iscoroutinefunction — это его дело, не наше.
    pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction':DeprecationWarning"),
]


class TestPydollChrome(DriverContractSuite["Browser", PydollContext, "Tab"]):
    @override
    def make_driver(self) -> Driver[Browser, PydollContext, Tab]:
        return PydollDriver()

    @override
    async def visit(self, page: Tab, url: str) -> str:
        await page.go_to(url, timeout=15)
        response = await page.execute_script("document.body.innerText", return_by_value=True)
        return str(response["result"]["result"]["value"])  # pyright: ignore[reportTypedDictNotRequiredAccess]

    @override
    async def evaluate(self, page: Tab, expression: str) -> object:
        response = await page.execute_script(expression, return_by_value=True)
        return response["result"]["result"].get("value")
