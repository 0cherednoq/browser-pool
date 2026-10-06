"""Отладка: вкладка, на которой случилась ошибка, остаётся открытой — посмотреть глазами.

`Debug.hold_on_error=N`: исключение арендатора пробрасывается сразу, а вкладка не закрывается N
секунд или до `pool.held_pages.release()`. Слот при этом честно занят — задержанная вкладка остаётся
арендой в учёте пула. Остановка пула отпускает всё сразу.
"""

from __future__ import annotations

import asyncio
import contextlib


class HeldPages:
    """Вкладки, задержанные после ошибки. `pool.held_pages`."""

    def __init__(self, hold: float | None) -> None:
        """`hold` — сколько секунд держать; `None` — не держать вовсе."""
        self._hold = hold
        self._release = asyncio.Event()
        self._held = 0
        self._closed = False

    @property
    def holding(self) -> bool:
        """Вкладки после ошибки задерживаются."""
        return self._hold is not None and not self._closed

    @property
    def held(self) -> int:
        """Сколько вкладок задержано сейчас."""
        return self._held

    def release(self) -> int:
        """Отпустить все задержанные вкладки сейчас. Сколько отпущено."""
        released = self._held
        self._release.set()
        self._release = asyncio.Event()
        return released

    def close(self) -> None:
        """Пул останавливается: отпустить всё и больше не держать."""
        self._closed = True
        self.release()

    async def hold(self) -> None:
        """Подержать вкладку: `hold` секунд или до `release()`."""
        if not self.holding or self._hold is None:
            return
        release = self._release
        self._held += 1
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._hold):
                    await release.wait()
        finally:
            self._held -= 1


__all__ = ["HeldPages"]
