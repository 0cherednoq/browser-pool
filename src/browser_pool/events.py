"""События пула: что происходит с браузерами, контекстами и арендами.

Каждое событие — неизменяемый dataclass с моментом в UTC (`at`) и только несекретными полями:
ключи identity, номера аренд, браузеры, виды сбоев, имена типов исключений. Текст чужого
исключения в событие не попадает: в нём может оказаться что угодно, от URL прокси с паролем до
значения куки.

Подписка — `pool.on(EventType, handler)`; подписка на `PoolEvent` получает всё. Обработчик
может быть синхронным или асинхронным (его выполнит фоновая задача пула, и остановка пула его
дождётся). Упавший обработчик пул не роняет — ошибка уходит в лог. Метрики (Prometheus, OTel)
строятся поверх событий.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

# В рантайме: псевдоним `Handler` вычисляется по запросу (`.__value__`), и имена должны существовать.
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from browser_pool.clock import utc_now

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from datetime import datetime

    from browser_pool.errors import ErrorKind
    from browser_pool.snapshot import PoolSnapshot

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class PoolEvent:
    """Базовое событие пула."""

    at: datetime = field(default_factory=utc_now)
    """Когда случилось, в UTC."""


# --- браузеры --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserStarted(PoolEvent):
    """Процесс браузера запущен (впервые, после простоя или восстановления)."""

    browser_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserQuarantined(PoolEvent):
    """Браузер умер или завис и выведен из работы целиком."""

    browser_id: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserRestarted(PoolEvent):
    """Браузер снова в строю после карантина или планового перезапуска."""

    browser_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserRecycled(PoolEvent):
    """Начат плановый перезапуск браузера."""

    browser_id: str
    reason: str
    """`leases` - по числу аренд, `uptime` - по времени жизни, `rss` - распух (`Resources`)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserIdleClosed(PoolEvent):
    """Простаивающий браузер закрыт; понадобится - запустится снова."""

    browser_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PoolUnavailable(PoolEvent):
    """Все браузеры в карантине, восстановление не идёт: ждать нечего."""


# --- контексты и identity --------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextOpened(PoolEvent):
    """Контекст identity открыт физически."""

    key: str
    browser_id: str
    generation: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextRetired(PoolEvent):
    """Контекст выведен из работы явно: арендатором или реакцией на сбой."""

    key: str
    generation: int
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextClosed(PoolEvent):
    """Контекст закрыт физически - по любой причине, включая вытеснение и простой."""

    key: str
    generation: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionOpened(PoolEvent):
    """`flow.open` прошёл: сессия identity открыта."""

    key: str
    restored: bool
    """В контекст было что восстановить - сохранённое или начальное состояние."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StateSaved(PoolEvent):
    """Состояние сессии сохранено в хранилище."""

    key: str
    version: int
    trigger: str
    """`open` - сразу после входа, `interval` - периодически, `close` - перед закрытием, `flow` - по просьбе flow."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StateExportFailed(PoolEvent):
    """Состояние не снялось с контекста (обычно - контекст уже мёртв); хранилище не тронуто."""

    key: str
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class StateSaveFailed(PoolEvent):
    """Состояние не записалось в хранилище; сессия в контексте жива, работа продолжается."""

    key: str
    error: str
    """Имя типа исключения хранилища."""
    trigger: str
    """Когда сохраняли - как у `StateSaved.trigger`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenFailed(PoolEvent):
    """Контекст identity не открылся; identity на паузе."""

    key: str
    error: str
    """Имя типа исключения."""
    retry_in: float
    """Через сколько секунд identity снова получит попытку."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourcePressure(PoolEvent):
    """Хост вошёл под давление памяти или CPU (`reason`) или вышел из него (`reason=None`)."""

    reason: str | None
    """Что давит; `None` - давление снято, пул снова растёт."""
    free_memory_mb: float | None
    cpu_percent: float | None
    """Средняя загрузка за окно `Resources.sample_window`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskRetried(PoolEvent):
    """`pool.run`: попытка задачи не удалась, будет следующая - с новой арендой."""

    key: str
    """Identity упавшей попытки."""
    attempt: int
    """Номер упавшей попытки, с единицы."""
    kind: ErrorKind
    """Вид сбоя, по которому решено повторить."""
    error: str
    """Имя типа исключения."""
    delay: float
    """Пауза перед следующей попыткой, секунды."""


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityCooledDown(PoolEvent):
    """Identity поставлена на паузу."""

    key: str
    seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityBlocked(PoolEvent):
    """Identity заблокирована до явного `unblock`."""

    key: str
    kind: ErrorKind | None
    """Почему: вид сбоя; `None` - заблокировали вручную (`pool.block`)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OrphansReaped(PoolEvent):
    """При старте добиты браузеры, которые пережили свой пул (процесс приложения умер)."""

    count: int


# --- окна ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowPlaced(PoolEvent):
    """Окно встало на место: в ячейку сетки или свёрнуто, потому что ячеек не хватило."""

    key: str
    """Чьё окно: ключ identity или `identity#вкладка` при окне на вкладку."""
    slot: int | None
    """Номер ячейки; `None` - окно свёрнуто."""
    x: int
    y: int
    width: int
    height: int


# --- прокси ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyFailed(PoolEvent):
    """Прокси не пропустил identity: источнику ушёл отчёт, контекст закрывается."""

    key: str
    proxy: str
    """Безопасное имя прокси (`Proxy.label`), без кредов."""
    reason: str
    """Имя типа исключения или вид сбоя."""
    retrying: bool
    """Будет ли открытие повторено с другим прокси."""


@dataclass(frozen=True, slots=True, kw_only=True)
class NoUsableProxy(PoolEvent):
    """Identity нужен прокси, а источник не дал ни одного пригодного."""

    key: str


# --- аренды ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseAcquired(PoolEvent):
    """Вкладка выдана."""

    lease_id: int
    key: str
    browser_id: str
    generation: int
    waited: float
    """Сколько секунд заявка ждала выдачи."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseReleased(PoolEvent):
    """Аренда завершена."""

    lease_id: int
    key: str
    browser_id: str
    held: float
    """Сколько секунд вкладка была в аренде."""
    outcome: ErrorKind | None
    """Вид сбоя, если аренда кончилась сбоем; `None` - штатно."""
    error: str | None = None
    """Имя типа исключения арендатора, если оно было."""
    evidence: str | None = None
    """Ссылка на улики сбоя от `EvidenceSink` (путь, идентификатор); `None` - не снимались."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AcquireWatchdog(PoolEvent):
    """Заявка ждёт выдачи дольше `acquire_watchdog`; ожидание продолжается."""

    waited: float
    candidates: tuple[str, ...]
    leases_active: int
    waiting: int


@dataclass(frozen=True, slots=True, kw_only=True)
class LeakSuspected(PoolEvent):
    """Аренда держится дольше `leak_warn_after` - возможно, её забыли вернуть."""

    lease_id: int
    key: str
    held: float
    acquired_at: str = field(repr=False)
    """Стек места, где аренду взяли."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseRevoked(PoolEvent):
    """Аренда дольше `lease_max_duration`: задача-держатель отменена."""

    lease_id: int
    key: str
    held: float


@dataclass(frozen=True, slots=True, kw_only=True)
class PoolHealth(PoolEvent):
    """Снимок пула после очередной проверки здоровья - для публикации ёмкости наружу."""

    snapshot: PoolSnapshot


# --- шина ------------------------------------------------------------------------------

type Handler[E: PoolEvent] = Callable[[E], object | Awaitable[object]]


class EventBus:  # внутреннее: подписка — `pool.on`
    """Раздаёт события подписчикам. Сбой подписчика - в лог, дальше - как ни в чём не бывало."""

    def __init__(self, spawn: Callable[[Coroutine[Any, Any, None]], object]) -> None:
        self._spawn = spawn
        self._handlers: list[tuple[type[PoolEvent], Handler[Any]]] = []

    def on[E: PoolEvent](self, kind: type[E], handler: Handler[E]) -> Callable[[], None]:
        """Подписаться на события типа `kind` и его наследников. Возвращает отписку."""
        entry: tuple[type[PoolEvent], Handler[Any]] = (kind, handler)
        self._handlers.append(entry)

        def unsubscribe() -> None:
            if entry in self._handlers:
                self._handlers.remove(entry)

        return unsubscribe

    def emit(self, event: PoolEvent) -> None:
        """Отдать событие подписчикам."""
        for kind, handler in tuple(self._handlers):
            if not isinstance(event, kind):
                continue
            try:
                result = handler(event)
            except Exception:
                _logger.exception("Обработчик события %s упал", type(event).__name__)
                continue
            if inspect.isawaitable(result):
                self._spawn(_guarded(result, type(event).__name__))


async def _guarded(awaitable: Awaitable[object], name: str) -> None:
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.exception("Асинхронный обработчик события %s упал", name)


__all__ = [
    "AcquireWatchdog",
    "BrowserIdleClosed",
    "BrowserQuarantined",
    "BrowserRecycled",
    "BrowserRestarted",
    "BrowserStarted",
    "ContextClosed",
    "ContextOpened",
    "ContextRetired",
    "Handler",
    "IdentityBlocked",
    "IdentityCooledDown",
    "LeakSuspected",
    "LeaseAcquired",
    "LeaseReleased",
    "LeaseRevoked",
    "NoUsableProxy",
    "OpenFailed",
    "OrphansReaped",
    "PoolEvent",
    "PoolHealth",
    "PoolUnavailable",
    "ProxyFailed",
    "ResourcePressure",
    "SessionOpened",
    "StateExportFailed",
    "StateSaveFailed",
    "StateSaved",
    "TaskRetried",
    "WindowPlaced",
]
