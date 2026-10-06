"""Стенд: сайт почты за несколькими HTTP-прокси, и «падение» браузера по заказу."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from browser_pool.testing.contract_site import ContractProxy
from examples.accounts.harness.site import DemoSite

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from browser_pool.driver import Driver


@dataclass(frozen=True, slots=True)
class Stand:
    """Поднятый стенд: сайт и прокси, через которые он только и открывается."""

    site: DemoSite
    proxies: tuple[ContractProxy, ...]

    @property
    def proxy_ports(self) -> tuple[int, ...]:
        """Порты прокси на 127.0.0.1."""
        return tuple(proxy.port for proxy in self.proxies)

    def proxy_requests(self) -> dict[str, int]:
        """Сколько запросов прошло через каждый прокси."""
        return {f"proxy-{index}": len(proxy.hosts) for index, proxy in enumerate(self.proxies)}

    @classmethod
    @contextlib.contextmanager
    def up(cls, accounts: Mapping[str, str], *, proxies: int) -> Generator[Stand]:
        """Сайт с аккаунтами `{логин: пароль}` и `proxies` прокси перед ним."""
        with DemoSite(accounts) as site, contextlib.ExitStack() as stack:
            gateways = tuple(stack.enter_context(ContractProxy(site)) for _ in range(proxies))
            yield cls(site=site, proxies=gateways)


async def crash(driver: Driver[Any, Any, Any], browser: object) -> int | None:
    """Убить процесс браузера, как при падении: пул узнает об этом сам, по обрыву связи.

    `kill_browser` — операция драйвера, которой пул добивает зависшие браузеры; здесь её зовёт
    стенд в обход пула. Возвращает PID убитого процесса.
    """
    pid = driver.pid(browser)
    await driver.kill_browser(browser)
    return pid
