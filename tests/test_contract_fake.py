"""Контрактный набор драйвера на `FakeDriver`: фейк ведёт себя как настоящий SDK там, где это важно пулу.

Проверка «после закрытия нет процесса» пропускается: у фейка процессов нет.
"""

from __future__ import annotations

import dataclasses
from typing import override

from browser_pool.driver import Driver
from browser_pool.testing import (
    FAKE_CAPABILITIES,
    FakeBrowser,
    FakeContext,
    FakeDriver,
    FakePage,
)
from browser_pool.testing.contract import DriverContractSuite


class TestFakeDriver(DriverContractSuite[FakeBrowser, FakeContext, FakePage]):
    fake: FakeDriver
    has_process = False

    @override
    def make_driver(self) -> Driver[FakeBrowser, FakeContext, FakePage]:
        self.fake = FakeDriver()
        return self.fake

    @override
    async def visit(self, page: FakePage, url: str) -> str:
        return await self.fake.fetch(page, url)

    @override
    async def crash(
        self, driver: Driver[FakeBrowser, FakeContext, FakePage], browser: FakeBrowser
    ) -> None:
        self.fake.crash(browser)


class TestFakeDriverWithWindows(TestFakeDriver):
    """Тот же набор с окнами: фейк объявляет, что двигает их на лету."""

    @override
    def make_driver(self) -> Driver[FakeBrowser, FakeContext, FakePage]:
        capabilities = dataclasses.replace(
            FAKE_CAPABILITIES, window_control="runtime", new_window=True
        )
        self.fake = FakeDriver(capabilities=capabilities)
        return self.fake
