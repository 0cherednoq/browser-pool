"""Провайдеры эндпоинтов: откуда берётся браузер, если его запускает не драйвер.

Оба провайдера в поставке работают на стандартной библиотеке, поэтому импортируются из пакета::

    from browser_pool.providers import AdsPowerProvider, RemoteCDP
"""

from __future__ import annotations

from browser_pool.providers.adspower import AdsPowerError, AdsPowerProvider
from browser_pool.providers.remote_cdp import RemoteCDP

__all__ = ["AdsPowerError", "AdsPowerProvider", "RemoteCDP"]
