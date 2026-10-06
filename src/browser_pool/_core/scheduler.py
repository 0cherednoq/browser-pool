"""Планировщик: кому какую вкладку и в каком браузере — чистая логика, без I/O.

Планировщик ничего не запускает и не закрывает. Он ведёт логический учёт — браузеры, контексты
identity, выданные аренды, очередь ожидающих — и отвечает на вопрос «кому что можно выдать
сейчас». Физику выполняют слои над ним; они же сообщают ему о событиях. Поэтому он синхронный,
время получает параметром `now`, а его инварианты проверяются на случайных сценариях.

Правила:

- Identity живёт ровно в одном контексте, контекст — в одном браузере.
- Заявка — упорядоченный список кандидатов. Сначала пробуются кандидаты с уже открытым
  контекстом (вход дорог), потом первый по порядку получает новый контекст.
- Очередь без блокировки головы: заявка, которую нельзя выдать сейчас, не держит тех, кого
  можно; из тех, кому подходит один и тот же ресурс, его получает стоящий раньше. Приоритет —
  больше число, раньше в очереди; при равном — FIFO.
- Новый контекст — в самый свободный здоровый браузер: меньше занятых вкладок, затем меньше
  контекстов.
- Лимиты: вкладок на браузер (`pages_per_browser`), контекстов на браузер (сверх лимита
  вытесняется самый давно простаивающий; все заняты — ждать), вкладок на identity (строжайший
  из `Identity.max_pages`, `pages_per_identity`, ёмкости браузера), групповые потолки
  (`GroupLimit`: упёрлись в потолок контекстов — вытесняется простаивающий контекст группы).
- Вариант identity (через прокси или напрямую, имя в профиле) контекст не изолирует. Простаивающий
  контекст меняет вариант на месте, с новым поколением. Занятый — держится за заявку на другой
  вариант: текущему варианту новые вкладки не выдаются, иначе та заявка голодала бы вечно; когда
  ждущих другого варианта не осталось, удержание снимается.
- Контекст, выведенный из работы, новых аренд не получает, дорабатывает выданные и попадает в
  очередь на закрытие (`take_closures`). Identity получит новый контекст — следующего поколения —
  только когда старый закрыт (`forget`).
- Предел очереди (`max_waiting`) отклоняет новую заявку, которую нельзя выдать сразу, а не старые.
- Identity на паузе (`cool_down`, неудачное открытие) или заблокированная (`block`) пропускается:
  заявка на неё ждёт, а в `any_of` выдаётся другой кандидат. Статус identity живёт отдельно от
  контекста и переживает его закрытие.
- Счётчики контекста (аренды, возраст, счёт ошибок) выводят его из работы по порогам `Recycling`.
- Учёт по ключу identity не копится: поколения контекстов нумерует один счётчик на пул (номер растёт
  с каждым новым контекстом и нигде не повторяется), а статус identity живёт, пока в нём есть что
  помнить — пауза, блокировка, неудачные открытия. Счёт неудачных открытий забывается, когда после
  конца паузы прошло больше самой длинной паузы открытия: серия неудач кончилась.
- Выдать что-либо можно, только если в здоровом браузере есть свободная вкладка. Пока её нет,
  `assign` очередь не просматривает; появилась — просматривает до первой выдачи, занявшей её.
- Аренда контекста целиком (`exclusive`) выдаётся, когда у контекста нет других аренд, и
  занимает один слот вкладки. Пока она ждёт, новых вкладок этой identity не выдаётся (как с
  вариантом — иначе голодала бы); пока держится, контекст не выдаётся никому.
- Браузер с владельцем (`owner`: прокси на браузере, профиль вендора) принимает только
  контексты своего владельца. Браузер без контекстов — ничей. Чужой браузер освобождается
  вытеснением его простаивающих контекстов, а новый владелец получает его, только когда они
  закрыты (`forget`): физически браузер перезапускается под него. Смена варианта, которая
  меняет владельца, — не на месте: контекст выводится из работы и открывается заново.
- Рост можно закрыть (`allow_growth(False)`: хост под давлением): новых контекстов и смен варианта
  нет, открытые контексты обслуживают как обычно, заявки на новые ждут.

Фасад вызывает `assign` после каждого изменения, поэтому в очереди стоят только заявки, которые
нельзя обслужить сейчас, и новую заявку `request` вправе попробовать выдать сразу.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from browser_pool.config import Limits, Recovery, Recycling
from browser_pool.errors import PoolInvariantError, PoolSaturatedError
from browser_pool.snapshot import BrowserState, IdentityStatus

if TYPE_CHECKING:
    from collections.abc import Hashable, Sequence

    from browser_pool._core.capabilities import Owner
    from browser_pool.config import GroupLimit, Topology
    from browser_pool.identity import Identity

_NOBODY = object()
_STATUS_SWEEP = 30.0
"""Как часто `assign` убирает отжившие статусы identity, секунды."""
"""Владелец браузера без контекстов: годится любой identity."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Grant:
    """Выданная аренда вкладки: чья, в каком браузере, в каком поколении контекста."""

    lease_id: int
    browser_id: str
    identity: Identity
    generation: int
    """Поколение контекста identity. Операция по аренде другого поколения — устаревшая."""
    new_context: bool
    """Контекст создан или пересоздан этой выдачей (новая identity, смена варианта): его предстоит
    открыть физически, закрыв физический контекст прежнего поколения, если он есть."""
    exclusive: bool = False
    """Аренда контекста целиком: пока она не возвращена, контекст не выдаётся никому."""


@dataclass(eq=False, slots=True)
class Waiter:
    """Заявка в очереди. `grant` появляется, когда заявке выдана аренда."""

    candidates: tuple[Identity, ...]
    enqueued_at: float
    priority: int = 0
    exclusive: bool = False
    grant: Grant | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BrowserView:
    """Браузер глазами планировщика."""

    id: str
    state: BrowserState
    capacity: int
    active: int
    contexts: int
    leases_total: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextView:
    """Контекст identity глазами планировщика."""

    key: str
    browser_id: str
    generation: int
    active: int
    limit: int
    retiring: bool
    last_used: float
    labels: tuple[tuple[str, str], ...]


@dataclass(eq=False, slots=True)
class _Browser:
    id: str
    capacity: int
    state: BrowserState = BrowserState.healthy
    active: int = 0
    leases_total: int = 0


@dataclass(eq=False, slots=True)
class _Context:
    identity: Identity
    browser: _Browser
    generation: int
    limit: int
    last_used: float
    active: int = 0
    retiring: bool = False
    handed_for_closing: bool = False
    held_for: tuple[Hashable] | None = None
    """Вариант, ради которого контекст держится; в кортеже — сам вариант бывает `None`."""
    exclusive: bool = False
    """Контекст арендован целиком."""
    awaiting_exclusive: bool = False
    """Его ждёт аренда целиком: новых вкладок не выдавать, иначе она голодала бы вечно."""
    created_at: float = 0.0
    uses: int = 0
    error_score: float = 0.0

    def renew(self, generation: int, now: float) -> None:
        """Новое поколение на месте: счётчики — с нуля."""
        self.generation = generation
        self.created_at = now
        self.uses = 0
        self.error_score = 0.0


@dataclass(eq=False, slots=True)
class _Status:
    cooling_until: float = 0.0
    blocked: str | None = None
    open_failures: int = 0
    failed_generation: int = 0
    """Поколение контекста, сбой открытия которого уже учтён."""


@dataclass(eq=False, slots=True)
class _Lease:
    grant: Grant
    context: _Context


@dataclass(slots=True)
class _Counters:
    lease_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    generations: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    """Поколения контекстов — один счётчик на пул: по ключу identity ничего не копится."""
    variant_switches: int = 0


class Scheduler:
    """Логический учёт пула и решения «кому что выдать»."""

    def __init__(
        self,
        topology: Topology,
        *,
        limits: Limits | None = None,
        recycling: Recycling | None = None,
        recovery: Recovery | None = None,
        owner: Owner | None = None,
    ) -> None:
        capacity = topology.pages_per_browser
        self._topology = topology
        self._owner = owner
        self._growing = True
        self._limits = limits if limits is not None else Limits()
        self._recycling = recycling if recycling is not None else Recycling()
        self._recovery = recovery if recovery is not None else Recovery()
        self._statuses: dict[str, _Status] = {}
        self._next_sweep = 0.0
        self._capacity = capacity
        self._browsers = [
            _Browser(id=f"browser-{index}", capacity=capacity) for index in range(topology.browsers)
        ]
        self._contexts: dict[str, _Context] = {}
        self._leases: dict[int, _Lease] = {}
        self._queue: list[Waiter] = []
        self._closing: list[_Context] = []
        self._counters = _Counters()

    @property
    def active_leases(self) -> int:
        """Сколько аренд выдано и не возвращено."""
        return len(self._leases)

    @property
    def variant_switches(self) -> int:
        """Сколько раз контекст сменил вариант на месте."""
        return self._counters.variant_switches

    # --- заявки ------------------------------------------------------------------------

    def submit(
        self,
        candidates: Sequence[Identity],
        *,
        now: float,
        priority: int = 0,
        exclusive: bool = False,
    ) -> Waiter:
        """Поставить заявку в очередь по приоритету. Выдачу делает `assign`.

        `exclusive` — аренда контекста целиком: выдаётся, когда у контекста нет других аренд.
        """
        if not candidates:
            msg = "Заявка без кандидатов: нужна хотя бы одна identity"
            raise ValueError(msg)
        keys = [identity.key for identity in candidates]
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:
            msg = f"Заявка повторяет identity: {', '.join(repeated)}"
            raise ValueError(msg)
        waiter = Waiter(
            candidates=tuple(candidates), enqueued_at=now, priority=priority, exclusive=exclusive
        )
        place = next(
            (index for index, queued in enumerate(self._queue) if queued.priority < priority),
            len(self._queue),
        )
        self._queue.insert(place, waiter)
        return waiter

    def request(
        self,
        candidates: Sequence[Identity],
        *,
        now: float,
        priority: int = 0,
        exclusive: bool = False,
    ) -> Waiter:
        """Заявка с попыткой выдать сразу; нельзя, а очередь полна, — `PoolSaturatedError`."""
        waiter = self.submit(candidates, now=now, priority=priority, exclusive=exclusive)
        grant = self._try(waiter, now)
        if grant is not None:
            waiter.grant = grant
            self._queue.remove(waiter)
            return waiter
        limit = self._limits.max_waiting
        if limit is not None and len(self._queue) > limit:
            self._queue.remove(waiter)
            raise PoolSaturatedError(max_waiting=limit)
        return waiter

    def assign(self, *, now: float) -> list[Waiter]:
        """Выдать всё, что можно выдать сейчас, в порядке очереди. Возвращает обслуженные заявки."""
        self._release_orphan_holds()
        self._forget_stale_statuses(now)
        served: list[Waiter] = []
        if not self._has_room():
            return served  # свободных вкладок нет: выдать нечего, сколько очередь ни смотри
        for waiter in tuple(self._queue):
            grant = self._try(waiter, now)
            if grant is None:
                continue
            waiter.grant = grant
            self._queue.remove(waiter)
            served.append(waiter)
            if not self._has_room():
                break
        return served

    def _has_room(self) -> bool:
        """Есть ли свободная вкладка хоть в одном здоровом браузере: без неё не выдать ничего."""
        return any(
            browser.state is BrowserState.healthy and browser.active < browser.capacity
            for browser in self._browsers
        )

    @property
    def remembered_identities(self) -> frozenset[str]:
        """Ключи identity, о которых учёт что-то помнит: пауза, блокировка, неудачные открытия."""
        return frozenset(self._statuses)

    def has_context(self, key: str) -> bool:
        """Есть ли у identity контекст — рабочий или ещё не закрытый."""
        return key in self._contexts

    def cancel(self, waiter: Waiter) -> None:
        """Убрать заявку из очереди. Уже выданную аренду вызывающий возвращает `release`."""
        if waiter in self._queue:
            self._queue.remove(waiter)

    def abandon(self, waiter: Waiter, *, now: float) -> None:
        """Заявку бросили: убрать из очереди, а если аренду уже выдали, но не забрали, — вернуть."""
        if waiter in self._queue:
            self._queue.remove(waiter)
        elif waiter.grant is not None and waiter.grant.lease_id in self._leases:
            self.release(waiter.grant, now=now)

    def release(self, grant: Grant, *, now: float) -> None:
        """Вернуть аренду. Вторичный возврат — нарушение инварианта."""
        lease = self._leases.pop(grant.lease_id, None)
        if lease is None:
            msg = f"Аренда {grant.lease_id} уже возвращена или не выдавалась"
            raise PoolInvariantError(msg)
        context = lease.context
        if grant.exclusive:
            context.exclusive = False
        context.active -= 1
        context.browser.active -= 1
        context.last_used = now
        self._queue_closure_if_idle(context)

    def reconfigure(
        self,
        *,
        topology: Topology | None = None,
        limits: Limits | None = None,
        recycling: Recycling | None = None,
        recovery: Recovery | None = None,
    ) -> None:
        """Новые настройки — для следующих решений; выданные аренды не отбираются."""
        if topology is not None:
            self._topology = topology
            self._capacity = topology.pages_per_browser
            for browser in self._browsers:
                browser.capacity = self._capacity
        if limits is not None:
            self._limits = limits
        if recycling is not None:
            self._recycling = recycling
        if recovery is not None:
            self._recovery = recovery

    def add_browser(self, browser_id: str) -> None:
        """Новый слот браузера — сразу доступен для размещения."""
        if any(browser.id == browser_id for browser in self._browsers):
            msg = f"Браузер {browser_id} уже есть"
            raise PoolInvariantError(msg)
        self._browsers.append(_Browser(id=browser_id, capacity=self._capacity))

    def remove_browser(self, browser_id: str) -> None:
        """Убрать слот браузера. В нём не должно остаться ни аренд, ни контекстов."""
        browser = self._browser(browser_id)
        if browser.active or any(context.browser is browser for context in self._contexts.values()):
            msg = f"Браузер {browser_id} убирается, а в нём ещё аренды или контексты"
            raise PoolInvariantError(msg)
        self._browsers.remove(browser)

    def allow_growth(self, allowed: bool) -> None:  # noqa: FBT001 — да/нет и есть смысл
        """Открыть или закрыть рост: новые контексты и смены варианта (хост под давлением)."""
        self._growing = allowed

    # --- контексты ---------------------------------------------------------------------

    def retire(self, key: str, *, generation: int) -> bool:
        """Вывести контекст identity из работы. `False` — такого поколения уже нет."""
        context = self._contexts.get(key)
        if context is None or context.generation != generation or context.retiring:
            return False
        self._retire(context)
        return True

    def retire_coldest(self) -> bool:
        """Вывести из работы самый давно простаивающий контекст. `False` — простаивающих нет."""
        idle = [c for c in self._contexts.values() if not c.retiring and c.active == 0]
        if not idle:
            return False
        self._retire(min(idle, key=lambda context: context.last_used))
        return True

    def retire_idle(self, *, now: float, idle_ttl: float) -> int:
        """Вывести из работы контексты, простаивающие дольше `idle_ttl`. Возвращает, сколько."""
        stale = [
            context
            for context in self._contexts.values()
            if not context.retiring and context.active == 0 and context.last_used <= now - idle_ttl
        ]
        for context in stale:
            self._retire(context)
        return len(stale)

    def take_closures(self) -> list[ContextView]:
        """Контексты к физическому закрытию: выведены из работы и без аренд. Отдаются один раз."""
        closing, self._closing = self._closing, []
        return [_context_view(context) for context in closing]

    def forget(self, key: str, *, generation: int) -> None:
        """Контекст закрыт физически: identity снова может получить новый."""
        context = self._contexts.get(key)
        if context is None or context.generation != generation:
            return
        if not context.handed_for_closing:
            msg = f"Контекст {key} поколения {generation} забыт, не будучи отданным на закрытие"
            raise PoolInvariantError(msg)
        del self._contexts[key]

    def record_outcome(self, grant: Grant, *, failed: bool, now: float) -> None:
        """Итог аренды — для счётчиков контекста; порог достигнут — контекст выводится из работы."""
        lease = self._leases.get(grant.lease_id)
        if lease is None or lease.context.generation != grant.generation:
            return
        context = lease.context
        recycling = self._recycling
        if failed:
            context.error_score += 1
        else:
            context.error_score = max(
                0.0, context.error_score - recycling.context_error_score_decrement
            )
        worn_out = (
            (
                recycling.context_max_leases is not None
                and context.uses >= recycling.context_max_leases
            )
            or (
                recycling.context_max_age is not None
                and now - context.created_at >= recycling.context_max_age
            )
            or (
                recycling.context_max_error_score is not None
                and context.error_score >= recycling.context_max_error_score
            )
        )
        if worn_out and not context.retiring:
            self._retire(context)

    # --- статусы identity --------------------------------------------------------------

    def cool_down(self, key: str, *, until: float) -> None:
        """Пауза identity до момента `until`; более ранняя пауза не сокращает уже назначенную."""
        status = self._status(key)
        status.cooling_until = max(status.cooling_until, until)

    def open_failed(self, key: str, *, now: float) -> float:
        """Контекст identity не открылся: пауза, растущая вдвое. Возвращает её конец."""
        status = self._status(key)
        status.open_failures += 1
        pause = self._recovery.open_failure_backoff.delay(status.open_failures - 1)
        self.cool_down(key, until=now + pause)
        return status.cooling_until

    def first_open_failure(self, key: str, *, generation: int) -> bool:
        """Первый ли это сбой открытия контекста этого поколения.

        Остальные аренды поколения, выданные до сбоя, получат ту же ошибку: считать её ещё раз —
        значит растить паузу identity за один и тот же сбой.
        """
        status = self._status(key)
        if status.failed_generation == generation:
            return False
        status.failed_generation = generation
        return True

    def open_succeeded(self, key: str) -> None:
        """Контекст identity открылся: счёт неудач — с нуля."""
        status = self._statuses.get(key)
        if status is not None:
            status.open_failures = 0
            self._drop_if_blank(key, status)

    def block(self, key: str, *, reason: str) -> None:
        """Заблокировать identity до явного `unblock`."""
        self._status(key).blocked = reason

    def unblock(self, key: str) -> None:
        """Снять блокировку и паузу identity."""
        status = self._statuses.get(key)
        if status is None:
            return
        status.blocked = None
        status.cooling_until = 0.0
        self._drop_if_blank(key, status)

    def identity_status(self, key: str, *, now: float) -> IdentityStatus:
        """Статус identity сейчас."""
        status = self._statuses.get(key, _Status())
        return IdentityStatus(
            key=key,
            cooling_until=status.cooling_until if status.cooling_until > now else None,
            blocked=status.blocked,
            open_failures=status.open_failures,
        )

    def identity_statuses(self, *, now: float) -> list[IdentityStatus]:
        """Identity с особым статусом: на паузе, заблокированные или после неудачных открытий."""
        return [
            view
            for view in (self.identity_status(key, now=now) for key in self._statuses)
            if view.cooling_until is not None or view.blocked is not None or view.open_failures
        ]

    def next_wakeup(self, *, now: float) -> float | None:
        """Ближайший конец паузы среди identity, которых ждут, — когда снова пробовать выдачу."""
        wanted = {identity.key for waiter in self._queue for identity in waiter.candidates}
        ends = [
            self._statuses[key].cooling_until
            for key in wanted
            if key in self._statuses and self._statuses[key].cooling_until > now
        ]
        return min(ends, default=None)

    # --- браузеры ----------------------------------------------------------------------

    def set_browser_state(self, browser_id: str, state: BrowserState) -> None:
        """Сменить состояние браузера. Новые аренды получает только `healthy`."""
        self._browser(browser_id).state = state

    def retire_browser(self, browser_id: str) -> int:
        """Вывести из работы все контексты браузера: он уходит на перезапуск. Возвращает, сколько."""
        browser = self._browser(browser_id)
        doomed = [
            context
            for context in self._contexts.values()
            if context.browser is browser and not context.retiring
        ]
        for context in doomed:
            self._retire(context)
        return len(doomed)

    def browser(self, browser_id: str) -> BrowserView:
        """Один браузер."""
        return next(view for view in self.browsers() if view.id == browser_id)

    # --- наблюдение --------------------------------------------------------------------

    def waiting(self) -> list[Waiter]:
        """Заявки в очереди — в порядке очереди."""
        return list(self._queue)

    def browsers(self) -> list[BrowserView]:
        """Браузеры."""
        return [
            BrowserView(
                id=browser.id,
                state=browser.state,
                capacity=browser.capacity,
                active=browser.active,
                contexts=sum(context.browser is browser for context in self._contexts.values()),
                leases_total=browser.leases_total,
            )
            for browser in self._browsers
        ]

    def contexts(self) -> list[ContextView]:
        """Контексты identity, включая выводимые из работы."""
        return [_context_view(context) for context in self._contexts.values()]

    # --- выбор -------------------------------------------------------------------------

    def _try(self, waiter: Waiter, now: float) -> Grant | None:
        """Сначала кандидаты с открытым контекстом (вход дорог), потом новый контекст первому."""
        return self._try_open(waiter, now) or self._try_new(waiter, now)

    def _try_open(self, waiter: Waiter, now: float) -> Grant | None:
        for identity in waiter.candidates:
            context = self._contexts.get(identity.key)
            if context is None or not self._available(identity.key, now):
                continue
            grant = self._use_existing(context, identity, now, exclusive=waiter.exclusive)
            if grant is not None:
                return grant
        return None

    def _try_new(self, waiter: Waiter, now: float) -> Grant | None:
        if not self._growing:
            return None
        for identity in waiter.candidates:
            if identity.key in self._contexts or not self._available(identity.key, now):
                continue
            evict = self._group_admission(identity, None)
            if evict is None:
                continue
            browser = self._roomiest(identity)
            if browser is None:
                if self._no_browser_for_anyone(identity):
                    return None
                continue
            for context in evict:
                self._retire(context)
            context = self._new_context(identity, browser, now)
            return self._grant(context, identity, now, new_context=True, exclusive=waiter.exclusive)
        return None

    def _use_existing(
        self, context: _Context, identity: Identity, now: float, *, exclusive: bool
    ) -> Grant | None:
        switching = context.identity.variant != identity.variant
        if self._closed_to(context, identity, switching=switching, exclusive=exclusive):
            return None
        if switching and not self._growing:
            return None
        if switching and self._owner_of(identity) != self._owner_of(context.identity):
            # Другой владелец — другой запуск браузера: контекст открывается заново.
            self._retire(context)
            return None
        evict = self._group_admission(identity, context)
        if evict is None:
            return None
        for other in evict:
            self._retire(other)
        if switching:
            context.renew(next(self._counters.generations), now)
            context.held_for = None
            self._counters.variant_switches += 1
        return self._grant(context, identity, now, new_context=switching, exclusive=exclusive)

    def _closed_to(
        self, context: _Context, identity: Identity, *, switching: bool, exclusive: bool
    ) -> bool:
        """Нельзя выдать из контекста сейчас. Попутно ставит удержания против голодания."""
        if context.retiring or context.browser.state is not BrowserState.healthy:
            return True
        if context.exclusive or (context.awaiting_exclusive and not exclusive):
            return True
        if exclusive and context.active:
            context.awaiting_exclusive = True
            return True
        if switching and context.active:
            if context.held_for is None:
                context.held_for = (identity.variant,)
            return True
        if not switching and context.held_for is not None:
            return True
        return context.browser.active >= context.browser.capacity or context.active >= self._limit(
            identity
        )

    def _roomiest(self, identity: Identity) -> _Browser | None:
        owner = self._owner_of(identity)
        candidates = [
            browser
            for browser in self._browsers
            if browser.state is BrowserState.healthy
            and browser.active < browser.capacity
            and self._browser_owner(browser) in {_NOBODY, owner}
            and (
                self._live_contexts(browser) < self._topology.contexts_per_browser
                or self._evictable(browser)
            )
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda browser: (browser.active, self._live_contexts(browser)))

    def _free_foreign_browser(self, owner: Hashable) -> bool:
        """Освободить чужой браузер под нового владельца: вывести из работы его контексты.

        Годится здоровый браузер другого владельца, все контексты которого простаивают; из
        таких — простаивающий дольше всех. Свой браузер освобождать незачем: будь в нём место,
        его нашёл бы `_roomiest`. Браузер, который уже закрывает свои последние контексты,
        освобождать не нужно — его дождутся. `False` — освобождать нечего: все браузеры заняты.
        """
        idle: list[list[_Context]] = []
        for browser in self._browsers:
            contexts = self._idle_foreign(browser, owner)
            if contexts is None:
                continue
            if all(context.retiring for context in contexts):
                return True
            idle.append(contexts)
        if not idle:
            return False
        for context in min(idle, key=lambda contexts: max(c.last_used for c in contexts)):
            self._retire(context)
        return True

    def _idle_foreign(self, browser: _Browser, owner: Hashable) -> list[_Context] | None:
        """Контексты здорового браузера другого владельца, если все простаивают; иначе `None`."""
        contexts = self._contexts_in(browser)
        if browser.state is not BrowserState.healthy or not contexts:
            return None
        if self._browser_owner(browser) == owner or any(context.active for context in contexts):
            return None
        return contexts

    def _no_browser_for_anyone(self, identity: Identity) -> bool:
        """Новому контексту нет браузера: искать дальше бессмысленно?

        Без владельцев браузер годится всем, и раз его нет — его нет ни для кого. С
        владельцами другой кандидат может подойти к своему браузеру; а если освобождается
        чужой, заявка ждёт его.
        """
        return self._owner is None or self._free_foreign_browser(self._owner_of(identity))

    def _contexts_in(self, browser: _Browser) -> list[_Context]:
        return [context for context in self._contexts.values() if context.browser is browser]

    def _owner_of(self, identity: Identity) -> Hashable:
        return self._owner(identity) if self._owner is not None else None

    def _browser_owner(self, browser: _Browser) -> Hashable:
        """Владелец браузера — по любому его контексту, и выводимому тоже; нет контекстов — ничей.

        Ничей (`_NOBODY`) — не то же, что общий (`None`): в общий браузер не пойдёт identity с
        владельцем — например, с профилем на диске в пуле без владельцев у остальных.
        """
        for context in self._contexts.values():
            if context.browser is browser:
                return self._owner_of(context.identity)
        return _NOBODY

    def _new_context(self, identity: Identity, browser: _Browser, now: float) -> _Context:
        if self._live_contexts(browser) >= self._topology.contexts_per_browser:
            self._retire(min(self._evictable(browser), key=lambda context: context.last_used))
        context = _Context(
            identity=identity,
            browser=browser,
            generation=next(self._counters.generations),
            limit=self._limit(identity),
            last_used=now,
            created_at=now,
        )
        self._contexts[identity.key] = context
        return context

    def _grant(
        self,
        context: _Context,
        identity: Identity,
        now: float,
        *,
        new_context: bool,
        exclusive: bool = False,
    ) -> Grant:
        if exclusive:
            context.exclusive = True
            context.awaiting_exclusive = False
        context.identity = identity
        context.limit = self._limit(identity)
        context.active += 1
        context.uses += 1
        context.last_used = now
        context.browser.active += 1
        context.browser.leases_total += 1
        grant = Grant(
            lease_id=next(self._counters.lease_ids),
            browser_id=context.browser.id,
            identity=identity,
            generation=context.generation,
            new_context=new_context,
            exclusive=exclusive,
        )
        self._leases[grant.lease_id] = _Lease(grant=grant, context=context)
        return grant

    def _limit(self, identity: Identity) -> int:
        limits = (identity.max_pages, self._topology.pages_per_identity, self._capacity)
        return min(limit for limit in limits if limit is not None)

    # --- группы ------------------------------------------------------------------------

    def _groups_of(self, identity: Identity) -> list[GroupLimit]:
        return [
            group
            for group in self._limits.groups
            if identity.labels.get(group.label) == group.value
        ]

    def _group_members(self, group: GroupLimit) -> list[_Context]:
        return [
            context
            for context in self._contexts.values()
            if not context.retiring and context.identity.labels.get(group.label) == group.value
        ]

    def _group_pages(self, group: GroupLimit) -> int:
        return sum(
            context.active
            for context in self._contexts.values()
            if context.identity.labels.get(group.label) == group.value
        )

    def _group_admission(
        self, identity: Identity, context: _Context | None
    ) -> list[_Context] | None:
        """Пустят ли группы ещё одну вкладку `identity` в `context` (или в новый контекст).

        `None` — нет. Иначе — контексты групп, которые надо вытеснить ради места; пустой
        список — вытеснять никого не нужно. Существующий контекст тоже может вступать в группу:
        identity вправе принести другие метки (например, при смене варианта).
        """
        evict: list[_Context] = []
        for group in self._groups_of(identity):
            member = context is not None and _in_group(context.identity, group)
            if not self._group_has_page_room(group, context, member=member):
                return None
            if member or group.max_contexts is None:
                continue
            crowded, victim = self._group_context_room(group, context, evict)
            if not crowded:
                continue
            if victim is None:
                return None
            evict.append(victim)
        return evict

    def _group_has_page_room(
        self, group: GroupLimit, context: _Context | None, *, member: bool
    ) -> bool:
        if group.max_pages is None:
            return True
        # Вступающий контекст приносит в группу и свои уже занятые вкладки.
        joining = context.active if context is not None and not member else 0
        return self._group_pages(group) + joining + 1 <= group.max_pages

    def _group_context_room(
        self, group: GroupLimit, context: _Context | None, evict: list[_Context]
    ) -> tuple[bool, _Context | None]:
        """(группа полна контекстов, кого вытеснить — самого давно простаивающего или никого)."""
        assert group.max_contexts is not None  # noqa: S101 — вызывается только для потолка контекстов
        remaining = [
            other
            for other in self._group_members(group)
            if other is not context and other not in evict
        ]
        if len(remaining) < group.max_contexts:
            return False, None
        idle = [other for other in remaining if other.active == 0]
        return True, (min(idle, key=lambda other: other.last_used) if idle else None)

    # --- служебное ---------------------------------------------------------------------

    def _available(self, key: str, now: float) -> bool:
        status = self._statuses.get(key)
        return status is None or (status.blocked is None and status.cooling_until <= now)

    def _status(self, key: str) -> _Status:
        return self._statuses.setdefault(key, _Status())

    def _drop_if_blank(self, key: str, status: _Status) -> None:
        """Статус, в котором помнить нечего, не хранится: identity бывают одноразовыми."""
        if status.blocked is None and status.cooling_until == 0.0 and not status.open_failures:
            del self._statuses[key]

    def _forget_stale_statuses(self, now: float) -> None:
        """Убрать статусы, которые своё отжили. Не чаще раза в `_STATUS_SWEEP` секунд."""
        if now < self._next_sweep:
            return
        self._next_sweep = now + _STATUS_SWEEP
        memory = self._recovery.open_failure_backoff.maximum
        for key, status in tuple(self._statuses.items()):
            if status.blocked is not None or status.cooling_until > now:
                continue
            if status.open_failures and now - status.cooling_until <= memory:
                continue  # серия неудач ещё идёт: следующая пауза должна быть длиннее
            del self._statuses[key]

    def _release_orphan_holds(self) -> None:
        """Снять удержание варианта и аренды целиком, которых уже никто не ждёт."""
        if not any(
            context.held_for is not None or context.awaiting_exclusive
            for context in self._contexts.values()
        ):
            return  # удержаний нет — очередь перебирать незачем
        exclusive = {
            identity.key
            for waiter in self._queue
            if waiter.exclusive
            for identity in waiter.candidates
        }
        wanted = {
            (identity.key, identity.variant)
            for waiter in self._queue
            for identity in waiter.candidates
        }
        for context in self._contexts.values():
            held = context.held_for
            if held is not None and (context.identity.key, held[0]) not in wanted:
                context.held_for = None
            if context.awaiting_exclusive and context.identity.key not in exclusive:
                context.awaiting_exclusive = False

    def _retire(self, context: _Context) -> None:
        context.retiring = True
        context.held_for = None
        context.awaiting_exclusive = False
        self._queue_closure_if_idle(context)

    def _queue_closure_if_idle(self, context: _Context) -> None:
        if context.retiring and context.active == 0 and not context.handed_for_closing:
            context.handed_for_closing = True
            self._closing.append(context)

    def _live_contexts(self, browser: _Browser) -> int:
        return sum(
            context.browser is browser and not context.retiring
            for context in self._contexts.values()
        )

    def _evictable(self, browser: _Browser) -> list[_Context]:
        return [
            context
            for context in self._contexts.values()
            if context.browser is browser and not context.retiring and context.active == 0
        ]

    def _browser(self, browser_id: str) -> _Browser:
        for browser in self._browsers:
            if browser.id == browser_id:
                return browser
        msg = f"Неизвестный браузер {browser_id}"
        raise PoolInvariantError(msg)


def _in_group(identity: Identity, group: GroupLimit) -> bool:
    return identity.labels.get(group.label) == group.value


def _context_view(context: _Context) -> ContextView:
    return ContextView(
        key=context.identity.key,
        browser_id=context.browser.id,
        generation=context.generation,
        active=context.active,
        limit=context.limit,
        retiring=context.retiring,
        last_used=context.last_used,
        labels=tuple(sorted(context.identity.labels.items())),
    )


__all__ = ["BrowserView", "ContextView", "Grant", "Scheduler", "Waiter"]
