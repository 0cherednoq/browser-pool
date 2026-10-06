"""Хранилище сессий самого SDK: куки вошедшего пользователя — в файле на логин.

Так SDK живёт без пула: скрипт входит один раз, дальше восстанавливает куки из файла. В пуле это
режим «сессией владеет SDK» (`StatePolicy(mode="none")`): пул хранилище не трогает.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.async_api import BrowserContext


class SessionVault:
    """Куки по логинам в каталоге `root`: `<логин>.json`."""

    def __init__(self, root: Path) -> None:
        """Каталог создаётся при первой записи."""
        self.root = root

    async def restore(self, context: BrowserContext, username: str) -> bool:
        """Положить сохранённые куки в контекст. `False` — сохранённого нет."""
        path = self._path(username)
        if not await asyncio.to_thread(path.exists):
            return False
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        # Формат — тот же, что отдал `context.cookies()`: Playwright принимает его обратно.
        await context.add_cookies(json.loads(raw))
        return True

    async def save(self, context: BrowserContext, username: str) -> None:
        """Сохранить куки контекста; запись атомарная — оборванная не портит прежнюю."""
        cookies: list[Any] = list(await context.cookies())
        await asyncio.to_thread(self._write, username, json.dumps(cookies, ensure_ascii=False))

    def _write(self, username: str, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(username)
        draft = path.with_suffix(".tmp")
        draft.write_text(text, encoding="utf-8")
        draft.replace(path)

    def _path(self, username: str) -> Path:
        return self.root / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', username)}.json"
