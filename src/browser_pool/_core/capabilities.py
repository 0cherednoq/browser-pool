"""Планирование по возможностям драйвера: уровень изоляции выводится из capabilities.

- `proxy_scope="context"` — много identity в одном браузере, у каждой свой контекст и прокси;
- `"browser"` — прокси задаётся при запуске: браузер принадлежит своему **владельцу** и
  перезапускается под другого;
- `"external"` — браузер = профиль вендора: владелец — одна identity, прокси задаёт вендор.

Владелец браузера (`browser_owner`) — то, что зашито в запуск: прокси и отпечаток. Браузер
делят identity с одинаковым владельцем, и только если это вообще возможно (`shares_browser`):
драйвер создаёт контексты, а отпечаток висит не на браузере. Прокси заранее известен только у
`fixed` и `direct`; прокси из источника выбирается при открытии, поэтому такая identity —
владелец сама себе. Где делить нельзя, контекст в браузере один: `contexts_per_browser`
принудительно 1 (`effective_config`).

Профиль на диске (`StatePolicy.user_data_dir`) — тоже владелец: браузер запускается с этим
профилем, и в нём живёт только его identity (готовый контекст профиля). У остальных identity
того же пула владелец прежний — или никакого, если браузер общий.

Требования identity, которые драйвер не выполнит никогда (профиль на диске без
`persistent_dir`, прокси неподдерживаемой схемы), видны до постановки в очередь — `unmet`
перечисляет их все разом.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from browser_pool.driver import CONTEXT_SETTINGS
from browser_pool.provider import ProfileProvider

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable

    from browser_pool.config import PoolConfig
    from browser_pool.driver import DriverCapabilities
    from browser_pool.identity import Identity
    from browser_pool.provider import EndpointProvider
    from browser_pool.proxies import Proxy

_logger = logging.getLogger(__name__)

type Owner = Callable[[Identity], Hashable]
"""Владелец браузера, которого потребует identity: identity одного владельца делят браузер."""


def pair_capabilities(
    capabilities: DriverCapabilities, provider: EndpointProvider | None
) -> DriverCapabilities:
    """Возможности пары «драйвер + провайдер»: провайдер профилей меняет их (`adapt`)."""
    if isinstance(provider, ProfileProvider):
        return provider.adapt(capabilities)
    return capabilities


def browser_per_identity(capabilities: DriverCapabilities) -> bool:
    """Запуск браузера зависит от identity: прокси на браузере или профиль вендора."""
    return capabilities.proxy_scope != "context"


def shares_browser(capabilities: DriverCapabilities) -> bool:
    """Могут ли разные identity жить в одном браузере с прокси на браузере."""
    return (
        capabilities.proxy_scope == "browser"
        and capabilities.can_new_context
        and capabilities.fingerprint_scope not in {"browser", "external"}
    )


def browser_owner(capabilities: DriverCapabilities, *, proxy_source: bool) -> Owner | None:
    """Кому принадлежит браузер; `None` — никому: браузер общий (`proxy_scope="context"`).

    `proxy_source` — есть ли у пула источник прокси: без него `pool` и `sticky` — напрямую.
    Владелец identity может быть `None` и при заданной функции: identity без профиля в пуле,
    где профили есть, живёт в общих браузерах.
    """
    base = _launch_owner(capabilities, proxy_source=proxy_source)
    if not capabilities.persistent_dir:
        return base

    def owner(identity: Identity) -> Hashable:
        if uses_profile(identity):
            return profile_owner(identity)
        return base(identity) if base is not None else None

    return owner


def uses_profile(identity: Identity) -> bool:
    """Живёт ли identity в профиле на диске: свой браузер, готовый контекст профиля."""
    return identity.state.user_data_dir is not None


def profile_owner(identity: Identity) -> Hashable:
    """Владелец браузера профиля: identity и её каталог. Две identity с одним каталогом — два
    владельца: второй браузер не получит профиль, пока его держит первый (файловый замок).
    Другой каталог у варианта той же identity — другой запуск."""
    return ("profile", identity.key, identity.state.user_data_dir)


def _launch_owner(capabilities: DriverCapabilities, *, proxy_source: bool) -> Owner | None:
    """Владелец по тому, что зашито в запуск помимо профиля: прокси и отпечаток."""
    if not browser_per_identity(capabilities):
        return None
    if not shares_browser(capabilities):
        return _identity_owner

    def owner(identity: Identity) -> Hashable:
        policy = identity.proxy
        if policy.mode == "fixed":
            return ("proxy", policy.proxy)
        if policy.mode == "direct" or (policy.mode in {"pool", "sticky"} and not proxy_source):
            return ("direct",)
        return _identity_owner(identity)

    return owner


def effective_config(config: PoolConfig, capabilities: DriverCapabilities) -> PoolConfig:
    """Конфиг, который пул действительно исполняет на этом драйвере."""
    topology = config.topology
    hint = capabilities.max_pages_hint
    if hint is not None and hint < topology.pages_per_browser:
        _logger.warning(
            "Драйвер выдерживает %d вкладок на браузер: pages_per_browser=%d вместо %d",
            hint,
            hint,
            topology.pages_per_browser,
        )
        topology = dataclasses.replace(
            topology,
            pages_per_browser=hint,
            warm_pages_per_identity=min(topology.warm_pages_per_identity, hint),
        )
    if (
        browser_per_identity(capabilities)
        and not shares_browser(capabilities)
        and topology.contexts_per_browser != 1
    ):
        _logger.info(
            "Браузер драйвера принадлежит одной identity: contexts_per_browser=1 вместо %d",
            topology.contexts_per_browser,
        )
        topology = dataclasses.replace(topology, contexts_per_browser=1)
    return config if topology is config.topology else config.replace(topology=topology)


def unmet(identity: Identity, capabilities: DriverCapabilities) -> tuple[str, ...]:
    """Чего драйвер не умеет из того, что требует identity. Пусто — выполнимо."""
    missing: list[str] = []
    if identity.state.user_data_dir is not None and not capabilities.persistent_dir:
        missing.append(f"{identity.key}: профиль на диске (user_data_dir) — драйвер его не умеет")
    missing.extend(_unapplied_settings(identity, capabilities))
    fixed = identity.proxy.proxy if identity.proxy.mode == "fixed" else None
    if fixed is not None and capabilities.proxy_scope != "external":
        missing.extend(
            f"{identity.key}: {problem}" for problem in proxy_problems(fixed, capabilities)
        )
    return tuple(missing)


def _unapplied_settings(identity: Identity, capabilities: DriverCapabilities) -> list[str]:
    """Настройки контекста identity, которые ей не достанутся: молча выдать контекст без них нельзя."""
    options = identity.context_options
    if options is None:
        return []
    wanted = sorted(name for name in CONTEXT_SETTINGS if getattr(options, name) is not None)
    if not wanted:
        return []
    if uses_profile(identity) or not capabilities.can_new_context:
        # Готовый контекст (профиль на диске, профиль вендора) драйвер не настраивает: он не пула.
        return [
            f"{identity.key}: настройки контекста ({', '.join(wanted)}) к готовому контексту "
            "профиля не применяются"
        ]
    return [
        f"{identity.key}: настройка контекста {name} — драйвер её не применяет"
        for name in wanted
        if name not in capabilities.context_settings
    ]


def proxy_problems(proxy: Proxy, capabilities: DriverCapabilities) -> tuple[str, ...]:
    """Почему драйвер не поднимет этот прокси; пусто — поднимет."""
    problems: list[str] = []
    if proxy.scheme not in capabilities.proxy_schemes:
        problems.append(
            f"схема прокси {proxy.scheme}: драйвер понимает "
            + ", ".join(sorted(capabilities.proxy_schemes))
        )
    if proxy.has_auth and not capabilities.proxy_auth:
        problems.append("авторизация на прокси логином и паролем")
    elif proxy.has_auth and proxy.scheme not in (
        capabilities.proxy_auth_schemes or capabilities.proxy_schemes
    ):
        problems.append(
            f"авторизация на прокси {proxy.scheme}: драйвер умеет её только для "
            + ", ".join(sorted(capabilities.proxy_auth_schemes or ()))
        )
    return tuple(problems)


def _identity_owner(identity: Identity) -> Hashable:
    return ("identity", identity.key)


__all__ = [
    "Owner",
    "browser_owner",
    "browser_per_identity",
    "effective_config",
    "pair_capabilities",
    "profile_owner",
    "proxy_problems",
    "shares_browser",
    "unmet",
    "uses_profile",
]
