"""Рабочая область экрана для раскладки окон.

`screen="auto"` — спросить у драйвера рабочую область монитора, где стоит окно первой вкладки
(`screen.avail*` страницы, без зависимостей). `Rect` или кортеж `Rect` — явно: несколько
мониторов заполняются по порядку. Номер монитора без сторонних библиотек не узнать на всех ОС —
он пока означает `auto` с предупреждением.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from browser_pool.geometry import Rect

if TYPE_CHECKING:
    from browser_pool.config import Screen
    from browser_pool.driver import WindowControl

_logger = logging.getLogger(__name__)


def explicit_areas(screen: Screen) -> list[Rect] | None:
    """Области, заданные в конфиге явно; `None` — спрашивать у драйвера."""
    if isinstance(screen, Rect):
        return [screen]
    if isinstance(screen, tuple):
        return list(screen)
    return None


async def resolve_areas[B, P](
    screen: Screen, *, control: WindowControl[B, P], page: P
) -> list[Rect]:
    """Рабочие области для раскладки: из конфига или у драйвера по вкладке."""
    areas = explicit_areas(screen)
    if areas is not None:
        return areas
    if isinstance(screen, int):
        _logger.warning(
            "Окна: номер монитора (%d) пока не поддерживается — беру монитор первой вкладки", screen
        )
    return [await control.screen_area(page)]


__all__ = ["explicit_areas", "resolve_areas"]
