"""Геометрия и геопозиция: значения, общие для конфига, identity и протокола драйвера.

Отдельный модуль без зависимостей: `config` и `identity` не обязаны знать про протокол драйвера,
чтобы описать окно, размер страницы или место, откуда «заходит» аккаунт.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_MAX_LATITUDE = 90.0
_MAX_LONGITUDE = 180.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Rect:
    """Прямоугольник на экране в пикселях: левый верхний угол и размер."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            if getattr(self, name) <= 0:
                msg = f"{name} прямоугольника должен быть положительным, получено {getattr(self, name)}"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Viewport:
    """Размер области страницы в пикселях."""

    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            if getattr(self, name) <= 0:
                msg = f"{name} viewport должен быть положительным, получено {getattr(self, name)}"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Geolocation:
    """Геопозиция, которую увидит страница."""

    latitude: float
    longitude: float
    accuracy: float | None = None
    """Точность в метрах."""

    def __post_init__(self) -> None:
        if not -_MAX_LATITUDE <= self.latitude <= _MAX_LATITUDE:
            msg = f"latitude вне [-90, 90]: {self.latitude}"
            raise ValueError(msg)
        if not -_MAX_LONGITUDE <= self.longitude <= _MAX_LONGITUDE:
            msg = f"longitude вне [-180, 180]: {self.longitude}"
            raise ValueError(msg)
        if self.accuracy is not None and not (math.isfinite(self.accuracy) and self.accuracy >= 0):
            msg = f"accuracy должна быть неотрицательной, получено {self.accuracy}"
            raise ValueError(msg)


__all__ = ["Geolocation", "Rect", "Viewport"]
