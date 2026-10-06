"""Мелкие помощники фасада пула: выбор кандидатов, слоты браузеров, связь аренды с пулом.

Вынесены из `pool.py`, чтобы фасад оставался перечнем операций, а не свалкой функций. Внутреннее.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from browser_pool._core.threads import call_in_thread
from browser_pool.driver import ContextSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from browser_pool._core.scheduler import BrowserView
    from browser_pool.driver import Driver
    from browser_pool.host import HostProbe
    from browser_pool.identity import Identity
    from browser_pool.proxies import Proxy
    from browser_pool.state import Cookie


@dataclass(frozen=True, slots=True)
class Natives[B, C]:
    """Что арендатор получает вместе с арендой, кроме вкладки."""

    browser: B
    context: C
    session: object = field(kw_only=True)
    proxy: Proxy | None = field(kw_only=True)


class LeaseLink:
    """Что аренда просит у пула (`LeaseControl`): пул не выставляет этих методов в своём API."""

    def __init__(
        self,
        driver: Driver[Any, Any, Any],
        *,
        attachments: Callable[[int], dict[str, object]],
        retire: Callable[..., Awaitable[None]],
        cookies: Callable[..., Awaitable[tuple[Cookie, ...]]],
    ) -> None:
        self._driver = driver
        self._attachments = attachments
        self._retire = retire
        self._cookies = cookies

    def attachments_of(self, lease_id: int) -> dict[str, object]:
        """Вложения вкладки аренды."""
        return self._attachments(lease_id)

    async def retire_context(self, key: str, *, generation: int, reason: str) -> None:
        """Вывести контекст identity этого поколения из работы."""
        await self._retire(key, generation=generation, reason=reason)

    async def cookies_of(self, lease_id: int, *, domain: str | None) -> tuple[Cookie, ...]:
        """Куки контекста аренды."""
        return await self._cookies(lease_id, domain=domain)

    async def call_in[**A, T](
        self, target: object, fn: Callable[A, T], /, *args: A.args, **kwargs: A.kwargs
    ) -> T:
        """Sync-функция в потоке браузера `target` (`thread_affinity`); без потоков — здесь же."""
        return await call_in_thread(self._driver, target, fn, *args, **kwargs)


def key_of(identity: Identity | str) -> str:
    """Ключ identity: методы статуса принимают и объект, и строку."""
    return identity if isinstance(identity, str) else identity.key


def slot_index(browser_id: str) -> int:
    """Номер слота браузера: `browser-3` → 3."""
    suffix = browser_id.rpartition("-")[2]
    return int(suffix) if suffix.isdigit() else 0


def removal_order(view: BrowserView) -> tuple[int, int, int]:
    """Кого убирать первым: меньше аренд, меньше контекстов, слот с большим номером."""
    return (view.active, view.contexts, -slot_index(view.id))


def new_slots(existing: Sequence[BrowserView], current: int, target: int) -> list[str]:
    """Номера новых слотов — наименьшие свободные (убираемые ещё заняты)."""
    taken = {view.id for view in existing}
    fresh: list[str] = []
    index = 0
    while current + len(fresh) < target:
        browser_id = f"browser-{index}"
        if browser_id not in taken:
            fresh.append(browser_id)
        index += 1
    return fresh


def default_probe() -> HostProbe | None:
    """Замеры хоста на psutil, если он установлен (extra `[resources]`).

    Адаптер грузится по имени: фасад не зависит от слоя адаптеров, а psutil — необязательная
    зависимость, которую ядро само не импортирует.
    """
    try:
        module = importlib.import_module("browser_pool.monitors.psutil")
    except ImportError:
        return None
    factory = cast("Callable[[], HostProbe | None]", module.default_probe)
    return factory()


def first_key(identity: Identity | None, any_of: Sequence[Identity] | None) -> str:
    """Ключ для события, когда аренда не выдалась: первый кандидат (список уже проверен)."""
    if identity is not None:
        return identity.key
    return any_of[0].key if any_of else ""


def first_failure(group: BaseExceptionGroup[Exception]) -> Exception:
    """Первое исключение группы, развёрнутое из вложенных групп."""
    first = group.exceptions[0]
    return first_failure(first) if isinstance(first, BaseExceptionGroup) else first


def candidates_of(
    identity: Identity | None, any_of: Sequence[Identity] | None
) -> tuple[Identity, ...]:
    """Кандидаты заявки: ровно одно из `identity` и `any_of`, не пустое."""
    if (identity is None) == (any_of is None):
        msg = "Укажите либо identity, либо any_of — ровно одно"
        raise ValueError(msg)
    candidates = (identity,) if identity is not None else tuple(any_of or ())
    if not candidates:
        msg = "any_of без identity: нужна хотя бы одна"
        raise ValueError(msg)
    return candidates


def context_spec(identity: Identity, *, fit_window: bool, window_per_page: bool) -> ContextSpec:
    """Заготовка `ContextSpec` из настроек контекста identity и окон."""
    options = identity.context_options
    if options is None:
        return ContextSpec(fit_window=fit_window, window_per_page=window_per_page)
    return ContextSpec(
        fit_window=fit_window,
        window_per_page=window_per_page,
        locale=options.locale,
        timezone=options.timezone,
        geolocation=options.geolocation,
        viewport=options.viewport,
        user_agent=options.user_agent,
        extra=dict(options.extra),
    )


__all__ = [
    "LeaseLink",
    "Natives",
    "candidates_of",
    "context_spec",
    "default_probe",
    "first_failure",
    "first_key",
    "key_of",
    "new_slots",
    "removal_order",
    "slot_index",
]
