"""Стенд примера — не часть приложения: локальный сайт почты, HTTP-прокси, «падение» браузера.

В настоящем проекте на этом месте живой сайт, настоящие прокси и настоящие падения.
"""

from __future__ import annotations

from examples.accounts.harness.site import HOST, DemoSite
from examples.accounts.harness.stand import Stand, crash

__all__ = ["HOST", "DemoSite", "Stand", "crash"]
