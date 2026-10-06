"""Повторы `pool.run`: новая аренда на попытку, повтор по виду сбоя.

Каждая попытка — новая аренда: пул уже отреагировал на сбой (выбросил вкладку, вывел контекст, сменит
прокси), и следующая получит исправное. Что повторять, решает вид сбоя (`retry_on`), а не тип исключения.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

from browser_pool.config import Backoff
from browser_pool.errors import ErrorKind, PoolError, ProxyFailedError, StaleLeaseError
from browser_pool.events import TaskRetried

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection
    from contextlib import AbstractAsyncContextManager

    from browser_pool._core.outcomes import Verdicts
    from browser_pool.events import PoolEvent
    from browser_pool.lease import PageLease

RETRIED_KINDS: frozenset[ErrorKind] = frozenset(
    {ErrorKind.page, ErrorKind.session, ErrorKind.proxy, ErrorKind.browser}
)
"""Что `pool.run` повторяет по умолчанию: сломалось окружение, а не identity и не задача."""


def retry_kind(
    error: Exception, lease: PageLease[Any, Any, Any, Any] | None, verdicts: Verdicts
) -> ErrorKind | None:
    """Вид сбоя попытки для решения о повторе; `None` — не повторять ни при каком `retry_on` (в том числе после `commit`)."""
    if lease is not None and lease.committed:
        return None  # точка невозврата: результат мог быть применён
    if isinstance(error, ProxyFailedError):
        return ErrorKind.proxy
    if isinstance(error, StaleLeaseError):
        return ErrorKind.page
    if isinstance(error, PoolError):
        return None
    verdict = verdicts.of(error, lease.reported if lease is not None else None)
    return verdict.kind if verdict is not None else None


async def run_with_retries[L: PageLease[Any, Any, Any, Any], T](
    open_lease: Callable[[], AbstractAsyncContextManager[L]],
    task: Callable[[L], Awaitable[T]],
    *,
    retries: int,
    backoff: Backoff | None,
    retry_on: Collection[ErrorKind],
    verdicts: Verdicts,
    emit: Callable[[PoolEvent], None],
    first_key: str,
) -> T:
    """Выполнить `task` на новой аренде; упало по повторяемой причине — ещё раз, с паузой `backoff`.

    `first_key` — identity для события, когда аренда не выдалась и назвать её нечем.
    """
    if retries < 0:
        msg = f"retries должен быть ≥ 0, получено {retries}"
        raise ValueError(msg)
    pause = backoff if backoff is not None else Backoff()
    attempt = 0
    while True:
        lease: L | None = None
        try:
            async with open_lease() as lease:
                return await task(lease)
        except Exception as error:
            kind = retry_kind(error, lease, verdicts)
            if kind is None or attempt >= retries or kind not in retry_on:
                raise
            delay = pause.delay(attempt, spread=random.random())  # noqa: S311 — разброс пауз
            attempt += 1
            key = lease.identity.key if lease is not None else first_key
            emit(
                TaskRetried(
                    key=key, attempt=attempt, kind=kind, error=type(error).__name__, delay=delay
                )
            )
            await asyncio.sleep(delay)


__all__ = ["RETRIED_KINDS", "retry_kind", "run_with_retries"]
