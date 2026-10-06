"""Источники прокси: откуда пул берёт прокси для контекста и куда сообщает, как он себя показал.

Пул выбирает прокси один раз на контекст: `acquire` → контекст живёт с ним → `release`. Между
ними — `report`: прокси не прошёл (`failed`) или сайт его забанил (`banned`), либо всё в порядке
(`ok`). Источник решает сам, что с этим делать: встроенный `ProxyList` ведёт выключатель, а
`CallbackProxySource` отдаёт отчёт приложению, у которого прокси лежат в своей БД.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, Self

from browser_pool._choice import check_choice
from browser_pool.clock import monotonic
from browser_pool.proxies.proxy import Proxy, ProxyFormatError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

_logger = logging.getLogger(__name__)

DIRECT = "direct"
"""Имя записи «без прокси» — в списках строк и в `ProxyLease.proxy_id`."""

type ProxyStrategy = Literal["round_robin", "least_used", "sticky"]
type OutcomeKind = Literal["ok", "failed", "banned"]

_STRATEGIES = frozenset({"round_robin", "least_used", "sticky"})
_MAX_BACKOFF_POWER = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyRequest:
    """Заявка пула на прокси для контекста identity.

    `exclude` — прокси, которые для этого открытия уже не прошли; `preferred` — прокси, с которым
    identity жила в прошлый раз (из её записи): источник отдаёт его, пока тот пригоден.
    `sticky` — identity закреплена за прокси (`ProxyPolicy.sticky()`): источник выбирает по ключу
    identity детерминированно, а не по своей стратегии, — так закрепление переживает перезапуск пула
    и не зависит от того, сохраняется ли состояние identity.
    """

    identity_key: str
    labels: Mapping[str, str] = field(default_factory=dict[str, str])
    exclude: frozenset[str] = frozenset()
    preferred: str | None = None
    sticky: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyLease:
    """Выданный прокси. `proxy is None` — контекст идёт напрямую, `proxy_id` тогда `"direct"`."""

    proxy: Proxy | None
    proxy_id: str
    identity_key: str

    @classmethod
    def of(cls, proxy: Proxy | None, identity_key: str) -> Self:
        """Аренда прокси (или прямого выхода) для identity."""
        return cls(proxy=proxy, proxy_id=proxy_id(proxy), identity_key=identity_key)


@dataclass(frozen=True, slots=True)
class ProxyOutcome:
    """Как прокси себя показал. `reason` — вид сбоя, без текста исключений и кредов."""

    kind: OutcomeKind
    reason: str | None = None

    def __post_init__(self) -> None:
        check_choice(self, "kind", OutcomeKind)

    @classmethod
    def ok(cls) -> Self:
        """Прокси отработал."""
        return cls("ok")

    @classmethod
    def failed(cls, reason: str) -> Self:
        """Прокси не прошёл: таймаут, отказ соединения, неверные креды."""
        return cls("failed", reason)

    @classmethod
    def banned(cls, reason: str) -> Self:
        """Сайт не пускает с этого адреса: прокси сразу уходит на паузу."""
        return cls("banned", reason)


class ProxySource(Protocol):
    """Откуда пул берёт прокси."""

    async def acquire(self, request: ProxyRequest) -> ProxyLease | None:
        """Прокси для контекста или `None`, если пригодного нет."""
        ...

    async def report(self, lease: ProxyLease, outcome: ProxyOutcome) -> None:
        """Как прокси себя показал."""
        ...

    async def release(self, lease: ProxyLease) -> None:
        """Контекст закрыт — прокси свободен."""
        ...


def proxy_id(proxy: Proxy | None) -> str:
    """Имя прокси в источнике: `label` или `"direct"`."""
    return DIRECT if proxy is None else proxy.label


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyStatus:
    """Здоровье записи списка прокси."""

    name: str
    """Безопасное имя (`Proxy.label`)."""
    resting_for: float | None
    """Сколько секунд паузы осталось; `None` — не на паузе."""
    failures: int
    """Сбоев подряд с последнего успеха."""
    trips: int
    """Срабатываний выключателя подряд (от них растёт пауза)."""
    identities: int
    """Сколько identity живёт на прокси сейчас."""


@dataclass(slots=True)
class _Health:
    failures: int = 0
    trips: int = 0
    resting_until: float | None = None
    holders: Counter[str] = field(default_factory=Counter[str])


class ProxyList:
    """Список прокси приложения со стратегией выбора и выключателем.

    - `round_robin` — по кругу; `least_used` — где меньше живых контекстов; `sticky` — identity
      держится за свой прокси (rendezvous-хеш: убрали прокси — переезжают только его жители).
      Это выбор источника для всех identity; закрепление отдельной identity — `ProxyPolicy.sticky()`:
      пул просит источник выбирать по ключу identity (`ProxyRequest.sticky`) при любой стратегии, а
      прокси из записи состояния идёт как `preferred`.
    - Выключатель: `breaker_failures` сбоев подряд или один бан → пауза `breaker_cooldown`,
      каждая следующая подряд — вдвое дольше (до 16×). Успех обнуляет счёт.
    - `max_identities_per_proxy` — сколько identity одновременно живёт на одном прокси этого
      списка. Это не `Limits.max_identities_per_proxy`: тот считает пул по всем источникам, этот —
      сам список; ограничения действуют вместе, меньшее срабатывает раньше.
    - Записи различаются именем (`Proxy.label`): `host:port` с разными логинами — разные прокси.
    - `status()` — здоровье записей; `ban()`, `add()`, `remove()` — правка на лету.
    - `None` в списке — выход напрямую.
    """

    def __init__(
        self,
        proxies: Iterable[Proxy | None],
        *,
        strategy: ProxyStrategy = "round_robin",
        breaker_failures: int = 3,
        breaker_cooldown: float = 600.0,
        max_identities_per_proxy: int | None = None,
    ) -> None:
        """Проверяет параметры: пустой список и повторы имён — ошибка конфигурации."""
        entries = list(proxies)
        if not entries:
            msg = "список прокси пуст: для работы без прокси передайте [None]"
            raise ValueError(msg)
        ids = [proxy_id(proxy) for proxy in entries]
        if len(set(ids)) != len(ids):
            duplicated = sorted({name for name in ids if ids.count(name) > 1})
            msg = f"прокси в списке повторяются: {', '.join(duplicated)}"
            raise ValueError(msg)
        if strategy not in _STRATEGIES:
            msg = (
                f"стратегия выбора прокси {strategy!r} неизвестна: {', '.join(sorted(_STRATEGIES))}"
            )
            raise ValueError(msg)
        if breaker_failures < 1:
            msg = f"breaker_failures должен быть ≥ 1: {breaker_failures}"
            raise ValueError(msg)
        if breaker_cooldown < 0:
            msg = f"breaker_cooldown не может быть отрицательным: {breaker_cooldown}"
            raise ValueError(msg)
        if max_identities_per_proxy is not None and max_identities_per_proxy < 1:
            msg = f"max_identities_per_proxy должен быть ≥ 1: {max_identities_per_proxy}"
            raise ValueError(msg)
        self._entries: dict[str, Proxy | None] = dict(zip(ids, entries, strict=True))
        self._health: dict[str, _Health] = {name: _Health() for name in ids}
        self._strategy: ProxyStrategy = strategy
        self._breaker_failures = breaker_failures
        self._breaker_cooldown = breaker_cooldown
        self._max_identities = max_identities_per_proxy
        self._cursor = 0
        self.skipped_lines: tuple[int, ...] = ()
        """Номера строк, пропущенных `parse_lines(skip_invalid=True)`."""

    @classmethod
    def parse_lines(
        cls,
        lines: Iterable[str],
        *,
        strategy: ProxyStrategy = "round_robin",
        breaker_failures: int = 3,
        breaker_cooldown: float = 600.0,
        max_identities_per_proxy: int | None = None,
        skip_invalid: bool = False,
    ) -> Self:
        """Список из строк: `#` — комментарий, пустые пропускаются, `direct` — без прокси.

        Одинаковые прокси в разной записи (`1.2.3.4:80` и `http://1.2.3.4:80`) остаются одним.
        Плохая строка — `ProxyFormatError` с её номером (с единицы, считая пустые и комментарии),
        без содержимого: в нём пароль. `skip_invalid=True` — пропустить плохие строки, а их номера
        оставить в `skipped_lines` и в предупреждении лога.
        """
        proxies, skipped = _parse_entries(lines, skip_invalid=skip_invalid)
        if skipped:
            _logger.warning("Список прокси: пропущены строки с ошибкой формата: %s", skipped)
        source = cls(
            proxies,
            strategy=strategy,
            breaker_failures=breaker_failures,
            breaker_cooldown=breaker_cooldown,
            max_identities_per_proxy=max_identities_per_proxy,
        )
        source.skipped_lines = tuple(skipped)
        return source

    def status(self) -> tuple[ProxyStatus, ...]:
        """Здоровье каждой записи по порядку: пауза, срабатывания, живущие на ней identity."""
        now = monotonic()
        result: list[ProxyStatus] = []
        for name in self._entries:
            health = self._health[name]
            resting = health.resting_until is not None and now < health.resting_until
            result.append(
                ProxyStatus(
                    name=name,
                    resting_for=health.resting_until - now
                    if resting and health.resting_until
                    else None,
                    failures=health.failures,
                    trips=health.trips,
                    identities=len(health.holders),
                )
            )
        return tuple(result)

    async def ban(self, proxy: Proxy | str, *, reason: str = "") -> None:
        """Прокси забанен сайтом (по версии приложения): сразу на паузу, как `ProxyOutcome.banned`.

        `proxy` — сам прокси или его имя (`Proxy.label`, `ProxyLease.proxy_id`).
        """
        _ = reason  # причина — для журнала приложения; в выключатель она не нужна
        health = self._health.get(proxy if isinstance(proxy, str) else proxy_id(proxy))
        if health is not None and not self._resting(health):
            self._trip(health)

    def add(self, proxy: Proxy | None) -> None:
        """Добавить запись на лету. Повтор имени — `ValueError`."""
        name = proxy_id(proxy)
        if name in self._entries:
            msg = f"прокси уже есть в списке: {name}"
            raise ValueError(msg)
        self._entries[name] = proxy
        self._health[name] = _Health()

    def remove(self, proxy: Proxy | str) -> None:
        """Убрать запись на лету. Живущие на ней контексты дорабатывают; новые её не получат.

        Последнюю запись убрать нельзя: список не бывает пустым.
        """
        name = proxy if isinstance(proxy, str) else proxy_id(proxy)
        if name not in self._entries:
            return
        if len(self._entries) == 1:
            msg = "в списке прокси должна остаться хотя бы одна запись"
            raise ValueError(msg)
        names = list(self._entries)
        if names.index(name) < self._cursor:
            self._cursor -= 1
        del self._entries[name]
        self._health.pop(name, None)
        self._cursor %= len(self._entries)

    def labels(self) -> tuple[str, ...]:
        """Безопасные имена записей по порядку — для логов и отладки."""
        return tuple(self._entries)

    async def acquire(self, request: ProxyRequest) -> ProxyLease | None:
        """Пригодный прокси по стратегии, `preferred` — первым; `None`, если пригодного нет."""
        now = monotonic()
        usable = [name for name in self._entries if self._usable(name, request, now)]
        if not usable:
            return None
        chosen = request.preferred if request.preferred in usable else self._choose(usable, request)
        self._health[chosen].holders[request.identity_key] += 1
        return ProxyLease.of(self._entries[chosen], request.identity_key)

    async def report(self, lease: ProxyLease, outcome: ProxyOutcome) -> None:
        """Выключатель: сбои подряд или бан уводят прокси на паузу, успех обнуляет счёт.

        Отчёты, пришедшие во время паузы (от контекстов, открытых до срабатывания), в счёт не идут:
        один инцидент — одно срабатывание.
        """
        health = self._health.get(lease.proxy_id)
        if health is None:
            return
        if outcome.kind == "ok":
            health.failures = 0
            health.trips = 0
            return
        if self._resting(health):
            return  # уже на паузе: поздние отчёты того же инцидента паузу не наращивают
        if outcome.kind == "banned":
            self._trip(health)
        else:
            health.failures += 1
            if health.failures >= self._breaker_failures:
                self._trip(health)

    async def release(self, lease: ProxyLease) -> None:
        """Контекст закрыт: identity больше не живёт на прокси. Повторный вызов безвреден."""
        health = self._health.get(lease.proxy_id)
        if health is not None and health.holders[lease.identity_key] > 0:
            health.holders[lease.identity_key] -= 1
            if not health.holders[lease.identity_key]:
                del health.holders[lease.identity_key]

    def _usable(self, name: str, request: ProxyRequest, now: float) -> bool:
        if name in request.exclude:
            return False
        health = self._health[name]
        if health.resting_until is not None and now < health.resting_until:
            return False
        return (
            self._max_identities is None
            or request.identity_key in health.holders
            or len(health.holders) < self._max_identities
        )

    def _choose(self, usable: list[str], request: ProxyRequest) -> str:
        if request.sticky:
            return max(usable, key=lambda name: _rendezvous(request.identity_key, name))
        match self._strategy:
            case "sticky":
                return max(usable, key=lambda name: _rendezvous(request.identity_key, name))
            case "least_used":
                return min(usable, key=lambda name: self._health[name].holders.total())
            case "round_robin":
                names = list(self._entries)
                order = names[self._cursor :] + names[: self._cursor]
                chosen = next(name for name in order if name in usable)
                self._cursor = (names.index(chosen) + 1) % len(names)
                return chosen

    def _resting(self, health: _Health) -> bool:
        return health.resting_until is not None and monotonic() < health.resting_until

    def _trip(self, health: _Health) -> None:
        backoff = 2 ** min(health.trips, _MAX_BACKOFF_POWER)
        health.resting_until = monotonic() + self._breaker_cooldown * backoff
        health.trips += 1
        health.failures = 0


def _parse_entries(
    lines: Iterable[str], *, skip_invalid: bool
) -> tuple[list[Proxy | None], list[int]]:
    """Записи списка из строк и номера пропущенных; повторы одной записи — один раз."""
    seen: set[str] = set()
    proxies: list[Proxy | None] = []
    skipped: list[int] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxy = None if line.lower() == DIRECT else Proxy.parse(line)
        except ProxyFormatError as error:
            if not skip_invalid:
                msg = f"строка {number}: {error}"
                raise ProxyFormatError(msg) from None
            skipped.append(number)
            continue
        key = DIRECT if proxy is None else proxy.url
        if key not in seen:
            seen.add(key)
            proxies.append(proxy)
    return proxies, skipped


def _rendezvous(identity_key: str, name: str) -> int:
    digest = hashlib.sha256(f"{identity_key}\0{name}".encode()).digest()
    return int.from_bytes(digest[:8])


type _Pick = Callable[[ProxyRequest], Awaitable[Proxy | None] | Proxy | None]
type _OnReport = Callable[[ProxyLease, ProxyOutcome], object]
type _OnRelease = Callable[[ProxyLease], object]


class CallbackProxySource:
    """Прокси выбирает приложение: функция `(ProxyRequest) -> Proxy | None`, sync или async.

    Для прокси из БД приложения и для sticky-сессий провайдеров вида `user-session-{key}`.
    Отчёты и освобождение уходят в `on_report` / `on_release`, если они заданы.
    """

    def __init__(
        self,
        pick: _Pick,
        *,
        on_report: _OnReport | None = None,
        on_release: _OnRelease | None = None,
    ) -> None:
        """Функции приложения; каждая может быть обычной или корутинной."""
        self._pick = pick
        self._on_report = on_report
        self._on_release = on_release

    async def acquire(self, request: ProxyRequest) -> ProxyLease | None:
        """Спросить приложение. `None` — пригодного прокси нет."""
        proxy = await _settle(self._pick(request))
        if proxy is None:
            return None
        return ProxyLease.of(proxy, request.identity_key)

    async def report(self, lease: ProxyLease, outcome: ProxyOutcome) -> None:
        """Передать отчёт приложению."""
        if self._on_report is not None:
            await _settle(self._on_report(lease, outcome))

    async def release(self, lease: ProxyLease) -> None:
        """Сообщить приложению, что прокси свободен."""
        if self._on_release is not None:
            await _settle(self._on_release(lease))


async def _settle[T](result: T | Awaitable[T]) -> T:
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = [
    "DIRECT",
    "CallbackProxySource",
    "OutcomeKind",
    "ProxyLease",
    "ProxyList",
    "ProxyOutcome",
    "ProxyRequest",
    "ProxySource",
    "ProxyStatus",
    "ProxyStrategy",
    "proxy_id",
]
