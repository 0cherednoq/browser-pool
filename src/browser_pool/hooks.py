"""Хуки жизненного цикла: сюда подключаются отпечатки, stealth, блокировка ресурсов.

В самой библиотеке нет ни одного «полезного» дефолта вроде stealth-аргументов запуска: всё такое —
явный хук. Точки:

- `before_launch(spec, browser_id)` — дописать `LaunchSpec` перед запуском браузера;
- `after_browser_started(browser, browser_id)` — браузер запущен;
- `before_context(spec, identity)` — дописать `ContextSpec` перед созданием контекста;
- `after_context_created(context, identity)` — контекст создан;
- `after_page_created(page, identity)` — вкладка создана (один раз: тёплая вкладка его не повторяет);
- `after_acquire(lease)` / `before_release(lease)` — каждая аренда.

Одинаковы для запуска и подключения к уже запущенному браузеру. Регистрация — декоратором
(`@pool.hooks.before_context`) или плагином: объектом с любым подмножеством этих методов
(`BrowserPool(..., plugins=[...])`). Вызываются по порядку регистрации; синхронные и
асинхронные. Сбой хука, создающего ресурс, закрывает этот ресурс и уходит к вызывающему.

Хуки меняют работу пула — в отличие от событий (`pool.on(...)`), которые только наблюдают.
Метод плагина с именем вида `before_*` / `after_*` / `pre_*` / `on_*`, которого нет среди точек, —
опечатка: `ConfigError` с подсказкой. Плагин без единой точки — тоже.
"""

from __future__ import annotations

import difflib
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from browser_pool.errors import ConfigError

if TYPE_CHECKING:
    from browser_pool.driver import ContextSpec, LaunchSpec
    from browser_pool.identity import Identity
    from browser_pool.lease import PageLease

type Hook[*Args] = Callable[[*Args], Awaitable[object] | object]

HOOK_POINTS = (
    "before_launch",
    "after_browser_started",
    "before_context",
    "after_context_created",
    "after_page_created",
    "after_acquire",
    "before_release",
)
"""Имена точек — они же имена методов плагина."""

_POINT_PREFIXES = ("before_", "after_", "pre_", "on_")


async def _run[*Args](hooks: list[Hook[*Args]], *args: *Args) -> None:
    for hook in tuple(hooks):
        result = hook(*args)
        if inspect.isawaitable(result):
            await result


def _plugin_points(plugin: object) -> list[str]:
    """Точки плагина. Метод, похожий на точку, но не точка, и плагин без точек — `ConfigError`."""
    name_of = type(plugin).__name__
    for name in dir(plugin):
        if name.startswith("_") or name in HOOK_POINTS:
            continue
        if name.startswith(_POINT_PREFIXES) and callable(getattr(plugin, name)):
            close = difflib.get_close_matches(name, HOOK_POINTS, n=1, cutoff=0.5)
            hint = f"; имелось в виду `{close[0]}`" if close else ""
            msg = (
                f"Плагин {name_of}: `{name}` не точка хуков{hint}. Точки: {', '.join(HOOK_POINTS)}"
            )
            raise ConfigError(msg)
    points = [name for name in HOOK_POINTS if callable(getattr(plugin, name, None))]
    if not points:
        msg = f"Плагин {name_of} не имеет ни одной точки хуков: {', '.join(HOOK_POINTS)}"
        raise ConfigError(msg)
    return points


class Hooks[B, C, P]:
    """Хуки одного пула. Декораторы возвращают функцию как есть — её можно звать и напрямую."""

    def __init__(self) -> None:
        self._before_launch: list[Hook[LaunchSpec, str]] = []
        self._after_browser_started: list[Hook[B, str]] = []
        self._before_context: list[Hook[ContextSpec, Identity]] = []
        self._after_context_created: list[Hook[C, Identity]] = []
        self._after_page_created: list[Hook[P, Identity]] = []
        self._after_acquire: list[Hook[PageLease[B, C, P, Any]]] = []
        self._before_release: list[Hook[PageLease[B, C, P, Any]]] = []

    def before_launch(self, hook: Hook[LaunchSpec, str]) -> Hook[LaunchSpec, str]:
        """Дописать спецификацию запуска браузера."""
        self._before_launch.append(hook)
        return hook

    def after_browser_started(self, hook: Hook[B, str]) -> Hook[B, str]:
        """Браузер запущен."""
        self._after_browser_started.append(hook)
        return hook

    def before_context(self, hook: Hook[ContextSpec, Identity]) -> Hook[ContextSpec, Identity]:
        """Дописать спецификацию контекста identity."""
        self._before_context.append(hook)
        return hook

    def after_context_created(self, hook: Hook[C, Identity]) -> Hook[C, Identity]:
        """Контекст создан."""
        self._after_context_created.append(hook)
        return hook

    def after_page_created(self, hook: Hook[P, Identity]) -> Hook[P, Identity]:
        """Вкладка создана."""
        self._after_page_created.append(hook)
        return hook

    def after_acquire(self, hook: Hook[PageLease[B, C, P, Any]]) -> Hook[PageLease[B, C, P, Any]]:
        """Аренда выдана — до кода арендатора."""
        self._after_acquire.append(hook)
        return hook

    def before_release(self, hook: Hook[PageLease[B, C, P, Any]]) -> Hook[PageLease[B, C, P, Any]]:
        """Аренда возвращается — после кода арендатора."""
        self._before_release.append(hook)
        return hook

    def add(self, plugin: object) -> None:
        """Зарегистрировать плагин: каждый его метод с именем точки — хук этой точки."""
        for point in _plugin_points(plugin):
            getattr(self, f"_{point}").append(getattr(plugin, point))


class HookRunner[B, C, P](Hooks[B, C, P]):
    """Внутреннее: вызов хуков ядром. Пользователю пула виден только `Hooks`."""

    async def run_before_launch(self, spec: LaunchSpec, browser_id: str) -> None:
        """Вызвать хуки `before_launch`."""
        await _run(self._before_launch, spec, browser_id)

    async def run_after_browser_started(self, browser: B, browser_id: str) -> None:
        """Вызвать хуки `after_browser_started`."""
        await _run(self._after_browser_started, browser, browser_id)

    async def run_before_context(self, spec: ContextSpec, identity: Identity) -> None:
        """Вызвать хуки `before_context`."""
        await _run(self._before_context, spec, identity)

    async def run_after_context_created(self, context: C, identity: Identity) -> None:
        """Вызвать хуки `after_context_created`."""
        await _run(self._after_context_created, context, identity)

    async def run_after_page_created(self, page: P, identity: Identity) -> None:
        """Вызвать хуки `after_page_created`."""
        await _run(self._after_page_created, page, identity)

    async def run_after_acquire(self, lease: PageLease[B, C, P, Any]) -> None:
        """Вызвать хуки `after_acquire`."""
        await _run(self._after_acquire, lease)

    async def run_before_release(self, lease: PageLease[B, C, P, Any]) -> None:
        """Вызвать хуки `before_release`."""
        await _run(self._before_release, lease)


__all__ = ["HOOK_POINTS", "Hook", "Hooks"]
