"""Провайдер эндпоинтов: откуда берётся браузер, если его запускает не драйвер.

Вторая ось рядом с драйвером: драйвер знает SDK, провайдер — где взять уже запущенный браузер
(удалённый CDP, профиль антидетекта, облако). Нет провайдера — `driver.launch()`. Есть —
`provider.start()` → `driver.attach(endpoint)`. Так «AdsPower + Playwright» и «удалённый CDP +
pydoll» собираются без адаптера на каждую пару.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from browser_pool.driver import DriverCapabilities, Endpoint, LaunchSpec
    from browser_pool.identity import Identity


@dataclass(frozen=True, slots=True, kw_only=True)
class EndpointRequest:
    """Что пул просит у провайдера: браузер для слота пула, при необходимости — для identity."""

    browser_id: str
    """Слот пула (`browser-0`…): провайдер с несколькими адресами раздаёт их по слотам."""
    identity: Identity | None
    """Владелец браузера — у драйверов, где браузер принадлежит одной identity (профиль
    антидетекта); `None` — браузер общий или запускается впрок (восстановление, `min_browsers`)."""
    spec: LaunchSpec
    """Спецификация запуска после хуков `before_launch`: headless, прокси браузера, окно, `extra`."""


@runtime_checkable
class EndpointProvider(Protocol):
    """Источник браузеров для пула: удалённый CDP, антидетект, облако.

    Пул зовёт `start` вместо `driver.launch` и подключается к эндпоинту через `driver.attach`.
    `stop` для каждого выданного эндпоинта пул зовёт ровно один раз — что бы ни случилось с
    браузером: не подключился, закрыт, добит, пул остановлен. Каждый вызов пул ограничивает
    своими таймаутами.
    """

    async def start(self, request: EndpointRequest) -> Endpoint:
        """Поднять (или выделить) браузер и сказать, куда подключаться."""
        ...

    async def stop(self, endpoint: Endpoint) -> None:
        """Освободить браузер эндпоинта: остановить профиль, вернуть адрес в запас."""
        ...

    async def reap_orphans(self) -> int:
        """При старте пула: освободить то, что осталось от прошлого запуска. Возвращает, сколько."""
        ...


@runtime_checkable
class ProfileProvider(EndpointProvider, Protocol):
    """Провайдер, чей браузер — профиль вендора (антидетект): отпечаток, прокси и куки его.

    Меняет возможности пары «драйвер + провайдер»: пул планирует по `adapt(driver.capabilities)`
    — браузер принадлежит одной identity, контекст — готовый контекст профиля
    (`ContextSpec.reuse_default`), а не новый.
    """

    def adapt(self, capabilities: DriverCapabilities) -> DriverCapabilities:
        """Возможности драйвера, подключённого к браузеру этого провайдера."""
        ...


__all__ = ["EndpointProvider", "EndpointRequest", "ProfileProvider"]
