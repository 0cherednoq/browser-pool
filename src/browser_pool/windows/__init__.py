"""Окна для отладки: раскладка без перекрытий и её применение через драйвер.

Модуль рядом с ядром и независим от него: раскладка не влияет на аренды.
"""

from __future__ import annotations

from browser_pool.windows.layout import LayoutEngine, LayoutKind, LayoutPolicy, Plan, Reflow

__all__ = ["LayoutEngine", "LayoutKind", "LayoutPolicy", "Plan", "Reflow"]
