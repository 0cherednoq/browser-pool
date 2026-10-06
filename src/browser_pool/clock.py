"""Часы пула — единственный законный источник настоящего времени в библиотеке.

Моменты внутри пула — монотонное время цикла событий: настенные часы прыгают (NTP, ручная
правка, сон машины), и TTL по ним ломаются. Берётся именно время цикла, а не `time.monotonic`:
его же используют `asyncio.sleep`, `asyncio.timeout` и `call_later`, и в тестах цикл с
виртуальным временем (`browser_pool.testing.VirtualTimeLoop`) двигает их все разом.

Наружу — в события и записи, которые переживают процесс, — момент уходит `datetime` в UTC.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime


def monotonic() -> float:
    """Монотонное время цикла событий, секунды. Только внутри запущенного цикла."""
    return asyncio.get_running_loop().time()


def utc_now() -> datetime:
    """Настенное время в UTC — для событий и записей, которые переживают процесс."""
    return datetime.now(UTC)


__all__ = ["monotonic", "utc_now"]
