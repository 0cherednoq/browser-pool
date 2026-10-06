"""Выбор SDK браузера: драйвер пула и клиент почты под тот же SDK. Импорт — лениво."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from browser_pool.driver import Driver
    from examples.accounts.app.flow import ClientFactory

type DriverName = Literal["playwright", "pydoll"]


def browser_sdk(
    name: DriverName, *, base_url: str, headless: bool
) -> tuple[Driver[Any, Any, Any], ClientFactory]:
    """Драйвер и фабрика клиента почты. SDK второго браузера может быть не установлен."""
    if name == "pydoll":
        from browser_pool.drivers.pydoll import PydollDriver  # noqa: PLC0415 — SDK может не стоять
        from examples.accounts.mail_sdk.pydoll import PydollMailClient  # noqa: PLC0415

        return PydollDriver(headless=headless), lambda tab: PydollMailClient(tab, base_url=base_url)

    from browser_pool.drivers.playwright import PlaywrightDriver  # noqa: PLC0415 — SDK может не стоять
    from examples.accounts.mail_sdk.playwright import PlaywrightMailClient  # noqa: PLC0415

    return PlaywrightDriver(headless=headless), lambda page: PlaywrightMailClient(
        page, base_url=base_url
    )
