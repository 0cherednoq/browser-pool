"""Уровни прокси: дешёвые — пока сайт пускает, дорогие — когда перестал (tiered proxies).

`TieredProxyList([[None], [dc1, dc2], [resi1, resi2]])` — уровни от дешёвого к дорогому. У каждой
группы identity (`labels["service"]`, а не домен запроса: identity пула знает свой сайт) свой
текущий уровень:

- доля сбоев за последние `window` исходов на текущем уровне не ниже `raise_at` — группа
  поднимается на уровень выше: сайт этот класс адресов не пускает;
- `lower_after` успехов подряд — группа пробует уровень ниже: может, он снова годится.

Внутри уровня прокси выбирает свой `ProxyList` со стратегией и выключателем. Нет пригодного на
текущем уровне — берётся ближайший выше, потом ниже. Отчёт засчитывается группе и тогда, когда он
пришёл после `release` (так ядро сообщает о сбое открытия). Уровень из одного прокси почти не
эскалирует: выключатель уводит прокси на паузу раньше, чем окно `window` наполнится, — группа и так
берёт соседний уровень, пока прокси отдыхает.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from browser_pool.proxies.sources import ProxyList, ProxyStrategy

if TYPE_CHECKING:
    from collections.abc import Iterable

    from browser_pool.proxies.proxy import Proxy
    from browser_pool.proxies.sources import ProxyLease, ProxyOutcome, ProxyRequest


_RECENT = 1024
"""Сколько недавно освобождённых выдач помнить: отчёт о сбое открытия приходит уже после `release`."""


@dataclass(slots=True)
class _Group:
    tier: int = 0
    outcomes: deque[bool] = field(default_factory=deque[bool])
    """Исходы на текущем уровне: `True` — прокси отработал."""
    streak: int = 0
    """Успехов подряд на текущем уровне."""


class TieredProxyList:
    """Источник прокси с уровнями. Реализует `ProxySource`."""

    def __init__(
        self,
        tiers: Iterable[Iterable[Proxy | None]],
        *,
        strategy: ProxyStrategy = "round_robin",
        window: int = 10,
        raise_at: float = 0.3,
        lower_after: int = 50,
        breaker_failures: int = 3,
        breaker_cooldown: float = 600.0,
        max_identities_per_proxy: int | None = None,
    ) -> None:
        """`tiers` — от дешёвого к дорогому; остальное — правила перехода и опции `ProxyList`."""
        lists = [
            ProxyList(
                tier,
                strategy=strategy,
                breaker_failures=breaker_failures,
                breaker_cooldown=breaker_cooldown,
                max_identities_per_proxy=max_identities_per_proxy,
            )
            for tier in tiers
        ]
        if not lists:
            msg = "уровней прокси нет: нужен хотя бы один"
            raise ValueError(msg)
        names = [name for tier in lists for name in tier.labels()]
        if len(set(names)) != len(names):
            msg = "прокси повторяются между уровнями: уровень прокси должен быть однозначным"
            raise ValueError(msg)
        if window < 1 or lower_after < 1 or not 0 < raise_at <= 1:
            msg = "window и lower_after ≥ 1, raise_at — доля в (0, 1]"
            raise ValueError(msg)
        self._tiers = lists
        self._tier_of = {name: index for index, tier in enumerate(lists) for name in tier.labels()}
        self._window = window
        self._raise_at = raise_at
        self._lower_after = lower_after
        self._groups: dict[str, _Group] = {}
        self._issued: dict[tuple[str, str], tuple[str, int]] = {}
        """(identity, прокси) → (группа, число живых контекстов): отчёт приходит без меток."""
        self._released: OrderedDict[tuple[str, str], str] = OrderedDict()
        """Недавно освобождённые выдачи: ядро зовёт `release` и только потом `report(failed)`."""

    def tier(self, service: str) -> int:
        """Текущий уровень группы (с нуля)."""
        group = self._groups.get(service)
        return group.tier if group is not None else 0

    async def acquire(self, request: ProxyRequest) -> ProxyLease | None:
        """Прокси текущего уровня группы; нет пригодного — ближайшего выше, потом ниже."""
        service = request.labels.get("service", "")
        current = self.tier(service)
        order = [*range(current, len(self._tiers)), *range(current - 1, -1, -1)]
        for index in order:
            lease = await self._tiers[index].acquire(request)
            if lease is not None:
                key = (lease.identity_key, lease.proxy_id)
                self._issued[key] = (service, self._issued.get(key, (service, 0))[1] + 1)
                return lease
        return None

    async def report(self, lease: ProxyLease, outcome: ProxyOutcome) -> None:
        """Отчёт уровню прокси и в статистику группы — если прокси с её текущего уровня."""
        index = self._tier_of.get(lease.proxy_id)
        if index is None:
            return
        await self._tiers[index].report(lease, outcome)
        service = self._service_of(lease)
        if service is None:
            return
        group = self._groups.setdefault(service, _Group())
        if index == group.tier:
            self._record(group, ok=outcome.kind == "ok")

    async def release(self, lease: ProxyLease) -> None:
        """Контекст закрыт: прокси свободен на своём уровне."""
        index = self._tier_of.get(lease.proxy_id)
        if index is not None:
            await self._tiers[index].release(lease)
        key = (lease.identity_key, lease.proxy_id)
        issued = self._issued.get(key)
        if issued is None:
            return
        service, count = issued
        if count > 1:
            self._issued[key] = (service, count - 1)
            return
        del self._issued[key]
        self._released[key] = service
        self._released.move_to_end(key)
        while len(self._released) > _RECENT:
            self._released.popitem(last=False)

    def _service_of(self, lease: ProxyLease) -> str | None:
        key = (lease.identity_key, lease.proxy_id)
        issued = self._issued.get(key)
        return issued[0] if issued is not None else self._released.get(key)

    def _record(self, group: _Group, *, ok: bool) -> None:
        group.outcomes.append(ok)
        while len(group.outcomes) > self._window:
            group.outcomes.popleft()
        group.streak = group.streak + 1 if ok else 0
        failures = group.outcomes.count(False)
        full = len(group.outcomes) >= self._window
        if full and failures / len(group.outcomes) >= self._raise_at:
            self._move(group, group.tier + 1)
        elif group.streak >= self._lower_after:
            self._move(group, group.tier - 1)

    def _move(self, group: _Group, tier: int) -> None:
        if 0 <= tier < len(self._tiers):
            group.tier = tier
        group.outcomes.clear()
        group.streak = 0


__all__ = ["TieredProxyList"]
