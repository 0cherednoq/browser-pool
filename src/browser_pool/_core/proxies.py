"""Прокси контекстов: какой прокси дать identity, куда сообщить о сбое, когда отпустить.

Прокси выбирает пул — один раз на контекст — по `ProxyPolicy` identity:

- `pool` — любой пригодный из источника; `sticky` — тот же, что в прошлый раз (`proxy_ref` из
  записи identity идёт источнику как `preferred`); источника нет — напрямую;
- `direct` — напрямую; `fixed` — ровно заданный; `external` — прокси задаёт вендор браузера.

Прокси, который драйвер не поднимет (схема, авторизация), и прокси, на котором уже живёт
`max_identities_per_proxy` других identity, возвращаются источнику и исключаются из заявки.
Открытия через один прокси идут не больше `concurrent_opens_per_proxy` разом: два входа с одного
адреса в одну секунду сайту заметны.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from browser_pool._core.capabilities import proxy_problems
from browser_pool.errors import NoUsableProxyError, UnsupportedRequirementError
from browser_pool.events import NoUsableProxy, ProxyFailed
from browser_pool.proxies import ProxyLease, ProxyOutcome, ProxyRequest

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from browser_pool.config import Limits
    from browser_pool.driver import DriverCapabilities
    from browser_pool.events import PoolEvent
    from browser_pool.identity import Identity
    from browser_pool.proxies import Proxy, ProxySource

_logger = logging.getLogger(__name__)

EXTERNAL = "external"
"""`proxy_id` контекста, прокси которого задаёт вендор браузера."""


@dataclass(frozen=True, slots=True)
class ProxyBinding:
    """Прокси контекста и откуда он: выданный источником возвращается источнику."""

    lease: ProxyLease
    sourced: bool

    @property
    def proxy(self) -> Proxy | None:
        """Прокси контекста; `None` — напрямую или у вендора."""
        return self.lease.proxy


class ProxyBroker:
    """Выдача, учёт и отчёты по прокси контекстов."""

    def __init__(
        self,
        source: ProxySource | None,
        *,
        capabilities: DriverCapabilities,
        limits: Limits,
        retries: int,
        emit: Callable[[PoolEvent], None],
    ) -> None:
        self._source = source
        self._capabilities = capabilities
        self._max_identities = limits.max_identities_per_proxy
        self._opens_per_proxy = limits.concurrent_opens_per_proxy
        self._retries = retries
        self._emit = emit
        self._holders: dict[str, Counter[str]] = {}
        self._gates: dict[str, asyncio.Semaphore] = {}

    def reconfigure(self, *, limits: Limits, retries: int) -> None:
        """Новые лимиты прокси — для следующих контекстов; идущие открытия доработают."""
        self._max_identities = limits.max_identities_per_proxy
        self._retries = retries
        if limits.concurrent_opens_per_proxy != self._opens_per_proxy:
            self._opens_per_proxy = limits.concurrent_opens_per_proxy
            self._gates.clear()

    async def bind(
        self, identity: Identity, *, preferred: str | None, exclude: frozenset[str] = frozenset()
    ) -> ProxyBinding:
        """Прокси для нового контекста identity. Нет пригодного — `NoUsableProxyError`."""
        key, policy = identity.key, identity.proxy
        if self._capabilities.proxy_scope == "external" or policy.mode == "external":
            return ProxyBinding(
                ProxyLease(proxy=None, proxy_id=EXTERNAL, identity_key=key), sourced=False
            )
        if policy.mode == "fixed" and policy.proxy is not None:
            self._require_supported(policy.proxy)
            return self._hold(ProxyBinding(ProxyLease.of(policy.proxy, key), sourced=False))
        if policy.mode == "direct" or self._source is None:
            return ProxyBinding(ProxyLease.of(None, key), sourced=False)
        request = ProxyRequest(
            identity_key=key,
            labels=identity.labels,
            exclude=exclude,
            preferred=preferred if policy.mode == "sticky" else None,
            sticky=policy.mode == "sticky",
        )
        return self._hold(
            ProxyBinding(await self._from_source(self._source, request), sourced=True)
        )

    def can_retry(self, identity: Identity, attempt: int) -> bool:
        """Можно ли переоткрыть контекст с другим прокси после `attempt` неудач."""
        return (
            self._source is not None
            and identity.proxy.mode in {"pool", "sticky"}
            and attempt <= self._retries
        )

    def gate(self, binding: ProxyBinding) -> AbstractAsyncContextManager[object]:
        """Ограничение одновременных открытий через один прокси."""
        if binding.proxy is None or self._opens_per_proxy is None:
            return contextlib.nullcontext()
        gate = self._gates.get(binding.lease.proxy_id)
        if gate is None:
            gate = self._gates[binding.lease.proxy_id] = asyncio.Semaphore(self._opens_per_proxy)
        return gate

    async def succeeded(self, binding: ProxyBinding) -> None:
        """Контекст открылся через прокси — источник обнуляет счёт сбоев."""
        await self._report(binding, ProxyOutcome.ok())

    async def failed(self, binding: ProxyBinding, *, reason: str, retrying: bool) -> None:
        """Прокси не пропустил: отчёт источнику и событие. Не бросает."""
        if binding.proxy is None:
            return
        await self._report(binding, ProxyOutcome.failed(reason))
        self._emit(
            ProxyFailed(
                key=binding.lease.identity_key,
                proxy=binding.proxy.label,
                reason=reason,
                retrying=retrying,
            )
        )

    async def release(self, binding: ProxyBinding) -> None:
        """Контекст закрыт: прокси свободен. Не бросает."""
        holders = self._holders.get(binding.lease.proxy_id)
        key = binding.lease.identity_key
        if binding.proxy is not None and holders is not None and holders[key] > 0:
            holders[key] -= 1
            if not holders[key]:
                del holders[key]
        if binding.sourced and self._source is not None:
            try:
                await self._source.release(binding.lease)
            except Exception:
                _logger.warning(
                    "Источник прокси упал на release %s", binding.lease.proxy_id, exc_info=True
                )

    # --- внутреннее --------------------------------------------------------------------

    async def _from_source(self, source: ProxySource, request: ProxyRequest) -> ProxyLease:
        """Пригодный прокси из источника: неподходящие возвращаются и исключаются из заявки."""
        exclude = set(request.exclude)
        while True:
            asked = ProxyRequest(
                identity_key=request.identity_key,
                labels=request.labels,
                exclude=frozenset(exclude),
                preferred=request.preferred,
                sticky=request.sticky,
            )
            lease = await source.acquire(asked)
            if lease is None or lease.proxy_id in exclude:
                if lease is not None:
                    await source.release(lease)
                self._emit(NoUsableProxy(key=request.identity_key))
                raise NoUsableProxyError(identity=request.identity_key)
            if lease.proxy is None or (
                not self._unsupported(lease.proxy) and self._has_room(lease)
            ):
                return lease
            await source.release(lease)
            exclude.add(lease.proxy_id)

    def _hold(self, binding: ProxyBinding) -> ProxyBinding:
        if binding.proxy is not None:
            holders = self._holders.setdefault(binding.lease.proxy_id, Counter[str]())
            holders[binding.lease.identity_key] += 1
        return binding

    def _has_room(self, lease: ProxyLease) -> bool:
        holders = self._holders.get(lease.proxy_id)
        return (
            self._max_identities is None
            or holders is None
            or lease.identity_key in holders
            or len(holders) < self._max_identities
        )

    def _unsupported(self, proxy: Proxy) -> tuple[str, ...]:
        return proxy_problems(proxy, self._capabilities)

    def _require_supported(self, proxy: Proxy) -> None:
        missing = self._unsupported(proxy)
        if missing:
            raise UnsupportedRequirementError(missing=missing)

    async def _report(self, binding: ProxyBinding, outcome: ProxyOutcome) -> None:
        if not binding.sourced or self._source is None:
            return
        try:
            await self._source.report(binding.lease, outcome)
        except Exception:
            _logger.warning(
                "Источник прокси упал на report %s", binding.lease.proxy_id, exc_info=True
            )


__all__ = ["EXTERNAL", "ProxyBinding", "ProxyBroker"]
