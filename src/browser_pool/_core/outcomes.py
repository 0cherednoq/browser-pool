"""Исход аренды: что сломалось и как пул реагирует на ресурсы.

Исключение арендатора пробрасывается наружу как есть: пул только реагирует на него. Что сломалось,
решает конвейер `Verdicts`: `lease.report` → вид, объявленный самим исключением или его причиной
(`PoolSignal`, `pool_error_kind`) → `classifier` пула → `classify` драйвера → `unknown`. Каждый шаг идёт
по цепочке причин снаружи внутрь (`error_chain`). `Outcomes` по вердикту выбрасывает вкладку, выводит
контекст, ставит identity на паузу или блокирует, отправляет браузер в карантин и сохраняет улики.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from browser_pool.clock import monotonic
from browser_pool.errors import (
    Classification,
    ErrorKind,
    declared_classification,
    error_chain,
)
from browser_pool.events import (
    ContextRetired,
    IdentityBlocked,
    IdentityCooledDown,
    LeaseReleased,
    OpenFailed,
)
from browser_pool.lease import ContextLease, PageLease

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from browser_pool._core.contexts import PhysicalResources
    from browser_pool._core.observer import Observer
    from browser_pool._core.scheduler import Grant, Scheduler
    from browser_pool._core.supervisor import Supervisor
    from browser_pool.config import PoolConfig
    from browser_pool.driver import Driver
    from browser_pool.errors import Classifier
    from browser_pool.events import PoolEvent
    from browser_pool.evidence import EvidenceSink

_logger = logging.getLogger(__name__)

_CONTEXT_FAULTS = frozenset({ErrorKind.session, ErrorKind.proxy, ErrorKind.blocked})
_COUNTED_FAULTS = frozenset(
    {ErrorKind.page, ErrorKind.unknown, ErrorKind.session, ErrorKind.proxy, ErrorKind.browser}
)


class Verdicts:
    """Что сломалось: вердикт по ошибке арендатора."""

    def __init__(self, driver: Driver[Any, Any, Any], classifier: Classifier | None) -> None:
        self._driver = driver
        self._classifier = classifier

    def of(
        self, error: BaseException | None, reported: Classification | None
    ) -> Classification | None:
        """Сообщённое → объявленное → классификаторы → `unknown`; `None` — реагировать не на что."""
        if reported is not None:
            return reported
        if not isinstance(error, Exception):
            # Отмена задачи — не сбой сайта: вкладку выбросят, но реагировать не на что.
            return None
        declared = declared_classification(error)
        if declared is not None:
            return declared
        for classifier in (self._classifier, self._driver.classify):
            verdict = None if classifier is None else _ask(classifier, error)
            if verdict is not None:
                return verdict
        return Classification(ErrorKind.unknown)

    def is_proxy_fault(self, error: BaseException) -> bool:
        """Ошибка — сбой прокси."""
        verdict = self.of(error, None)
        return verdict is not None and verdict.kind is ErrorKind.proxy


def _ask(classifier: Classifier, error: Exception) -> Classification | None:
    """Вердикт классификатора по первому звену цепочки, которое он узнал; `None` — не узнал ни одного.

    Упавший или ответивший чепухой классификатор не заслоняет ошибку арендатора: считается, что
    он промолчал.
    """
    for link in error_chain(error):
        try:
            verdict = classifier(link)
            if verdict is not None:
                return (
                    verdict
                    if isinstance(verdict, Classification)
                    else Classification(ErrorKind(verdict))
                )
        except Exception:
            _logger.exception(
                "Классификатор упал на %s; считаю, что промолчал", type(link).__name__
            )
            return None
    return None


def tag_identity(error: BaseException, key: str) -> None:
    """Подписать исключение открытия: чья это identity (для `any_of`). Тип исключения не меняется."""
    if not isinstance(error, Exception) or hasattr(error, "pool_identity"):
        return
    try:
        error.pool_identity = key  # pyright: ignore[reportAttributeAccessIssue]
    except (AttributeError, TypeError):
        return  # исключение со __slots__: остаётся заметка
    error.add_note(f"browser_pool: identity {key}")


class Outcomes[P]:
    """Реакция на исход аренды: вкладка, контекст, identity, браузер, улики, события."""

    def __init__(
        self,
        *,
        verdicts: Verdicts,
        driver: Driver[Any, Any, P],
        scheduler: Scheduler,
        supervisor: Supervisor,
        resources: PhysicalResources[Any, Any, P],
        observer: Observer,
        evidence: EvidenceSink | None,
        config: Callable[[], PoolConfig],
        emit: Callable[[PoolEvent], None],
        spawn: Callable[[Coroutine[object, object, None]], object],
        fail_blocked_waiters: Callable[[], None],
        give_back: Callable[[Grant], None],
    ) -> None:
        self._verdicts = verdicts
        self._driver = driver
        self._scheduler = scheduler
        self._supervisor = supervisor
        self._resources = resources
        self._observer = observer
        self._evidence = evidence
        self._config = config
        self._emit = emit
        self._spawn = spawn
        self._fail_blocked_waiters = fail_blocked_waiters
        self._give_back = give_back

    async def finish(
        self,
        grant: Grant,
        page: P | None,
        *,
        lease: ContextLease[Any, Any, Any],
        error: BaseException | None,
        acquired: float,
    ) -> None:
        """Аренда возвращается: вкладка — в запас или на выброс, учёт, событие, реакция по вердикту."""
        verdict = self._verdicts.of(error, lease.reported)
        kind = verdict.kind if verdict is not None else None
        discard = (
            (isinstance(lease, PageLease) and lease.discarding)
            or error is not None
            or (kind is not None and kind is not ErrorKind.rate_limited)
        )
        proof: str | None = None
        try:
            if page is not None and isinstance(error, Exception):
                proof = await self._capture(grant, page, error)
            if page is not None:
                await self._resources.release_page(grant, page, discard=discard)
        finally:
            if kind is None or kind in _COUNTED_FAULTS:
                self._scheduler.record_outcome(grant, failed=kind is not None, now=monotonic())
            self._emit(
                LeaseReleased(
                    lease_id=grant.lease_id,
                    key=grant.identity.key,
                    browser_id=grant.browser_id,
                    held=monotonic() - acquired,
                    outcome=kind,
                    error=type(error).__name__ if error is not None else None,
                    evidence=proof,
                )
            )
            if verdict is not None:
                self._react(grant, verdict, error)
            self._give_back(grant)

    def acquire_failed(self, grant: Grant, error: BaseException) -> None:
        """Вкладку под выданную аренду получить не удалось — упал браузер или не открылся контекст."""
        verdict = self._verdicts.of(error, None)
        key = grant.identity.key
        tag_identity(error, key)
        if verdict is not None and verdict.kind is ErrorKind.browser:
            self._supervisor.quarantine(grant.browser_id, f"browser: {type(error).__name__}")
        elif isinstance(error, Exception) and not self._resources.has_context(
            key, generation=grant.generation
        ):
            self._scheduler.retire(key, generation=grant.generation)
            if self._scheduler.first_open_failure(key, generation=grant.generation):
                self._open_failed(grant, verdict, error)
        self._give_back(grant)

    async def _capture(self, grant: Grant, page: P, error: Exception) -> str | None:
        """Улики сбоя в приёмник — до того, как вкладку выбросят. Не бросает."""
        sink = self._evidence
        if sink is None:
            return None
        try:
            async with asyncio.timeout(self._config().timeouts.close):
                evidence = await self._driver.capture(page)
                return await sink.save(
                    evidence,
                    key=grant.identity.key,
                    lease_id=grant.lease_id,
                    error=type(error).__name__,
                )
        except Exception as failure:  # noqa: BLE001 — улики не важнее аренды
            _logger.warning(
                "Улики аренды %d не сохранились (%s)", grant.lease_id, type(failure).__name__
            )
            return None

    def _react(self, grant: Grant, verdict: Classification, error: BaseException | None) -> None:
        key, kind = grant.identity.key, verdict.kind
        # Только вид и тип: текст чужого исключения может нести секрет, а причина попадёт в снимок.
        reason = f"{kind.value}: {type(error).__name__}" if error is not None else kind.value
        if kind in _CONTEXT_FAULTS and self._scheduler.retire(key, generation=grant.generation):
            self._emit(ContextRetired(key=key, generation=grant.generation, reason=reason))
        if kind is ErrorKind.blocked:
            self._scheduler.block(key, reason=reason)
            self._emit(IdentityBlocked(key=key, kind=kind))
            self._fail_blocked_waiters()
        elif kind is ErrorKind.rate_limited:
            pause = verdict.retry_after
            if pause is None:
                pause = self._config().recovery.rate_limited_cooldown
            self._scheduler.cool_down(key, until=monotonic() + pause)
            self._emit(IdentityCooledDown(key=key, seconds=pause))
        elif kind is ErrorKind.browser:
            self._supervisor.quarantine(grant.browser_id, reason)
        elif kind is ErrorKind.proxy:
            self._spawn(
                self._resources.proxy_failed(key, generation=grant.generation, reason=reason)
            )

    def _open_failed(self, grant: Grant, verdict: Classification | None, error: Exception) -> None:
        """Контекст не открылся. Сайт сказал, почему, — реакция по виду; иначе — пауза открытия."""
        key, now = grant.identity.key, monotonic()
        self._observer.counters.open_failures += 1
        if verdict is not None and verdict.kind in {ErrorKind.blocked, ErrorKind.rate_limited}:
            self._react(grant, verdict, error)
            return
        # Пауза identity, чтобы следующие заявки не повторяли вход подряд.
        until = self._scheduler.open_failed(key, now=now)
        self._emit(OpenFailed(key=key, error=type(error).__name__, retry_in=until - now))


__all__ = ["Outcomes", "Verdicts", "tag_identity"]
