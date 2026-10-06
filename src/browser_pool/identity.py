"""Identity — от чьего имени работает браузер, и её политики.

Для пула identity непрозрачна: ключ, вариант, политика прокси и состояния, потолок вкладок,
метки. Что стоит за ней — аккаунт, профиль, анонимный слот — знает только site SDK, которому
пул передаёт `payload` при открытии сессии.

Идентичность identity — это ключ и вариант: два объекта с одним ключом и вариантом — одна
identity, как бы ни отличался payload. Payload в `repr` не попадает: там креды.

Протоколы сессии (`SessionFlow`, `OpenRequest`, `BaseFlow`) определены в `browser_pool.flow`:
identity несёт свой flow, а flow получает identity — в рантайме их связывает только аннотация.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Self, override

from browser_pool._choice import check_choice

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping

    from browser_pool.flow import SessionFlow
    from browser_pool.geometry import Geolocation, Viewport
    from browser_pool.proxies import Proxy
    from browser_pool.state import SessionState

type ProxyMode = Literal["pool", "direct", "fixed", "sticky", "external"]
type StateMode = Literal["read_write", "read_only", "none"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProxyPolicy:
    """Откуда identity берёт прокси. Выбирает пул — один раз на контекст."""

    mode: ProxyMode = "pool"
    """`pool` — любой пригодный из источника на каждый новый контекст; `direct` — без прокси;
    `fixed` — ровно этот; `sticky` — один и тот же и после перезапуска процесса; `external` —
    прокси задаёт вендор браузера (антидетект), пул его не назначает."""
    proxy: Proxy | None = None
    """Прокси для режима `fixed`."""

    def __post_init__(self) -> None:
        check_choice(self, "mode", ProxyMode)
        if self.mode == "fixed" and self.proxy is None:
            msg = "ProxyPolicy 'fixed' требует proxy"
            raise ValueError(msg)
        if self.mode != "fixed" and self.proxy is not None:
            msg = (
                f"ProxyPolicy '{self.mode}' не принимает proxy: он задаётся только в режиме 'fixed'"
            )
            raise ValueError(msg)

    @classmethod
    def pool(cls) -> Self:
        """Любой пригодный прокси из источника пула."""
        return cls(mode="pool")

    @classmethod
    def direct(cls) -> Self:
        """Без прокси."""
        return cls(mode="direct")

    @classmethod
    def fixed(cls, proxy: Proxy) -> Self:
        """Ровно этот прокси — например, закреплённый за аккаунтом снаружи."""
        return cls(mode="fixed", proxy=proxy)

    @classmethod
    def sticky(cls) -> Self:
        """Один и тот же прокси для identity, и после перезапуска процесса тоже.

        Не зависит от `StatePolicy`: пул просит источник выбирать по ключу identity (`ProxyRequest.sticky`),
        а прокси из записи состояния, если она есть, идёт предпочтительным. `ProxyList(strategy="sticky")` —
        другое: это стратегия источника для всех identity, а не закрепление одной.
        """
        return cls(mode="sticky")

    @classmethod
    def external(cls) -> Self:
        """Прокси задаёт вендор браузера; пул его не назначает."""
        return cls(mode="external")


@dataclass(frozen=True, slots=True, kw_only=True)
class StatePolicy:
    """Как identity обходится с состоянием сессии в хранилище пула."""

    mode: StateMode = "read_write"
    """`read_write` — читать и сохранять; `read_only` — читать, не сохранять; `none` — не трогать хранилище."""
    initial: SessionState | None = None
    """С чего начать, если в хранилище пусто: например, сессия, которую принёс оператор."""
    user_data_dir: Path | None = None
    """Профиль на диске — тяжёлое состояние; у драйверов с `persistent_dir`. У identity свой браузер,
    запущенный с этим профилем; прокси — при запуске. Профиль занят файловым замком (`<каталог>.lock`
    рядом с ним), пока браузер жив: второй процесс или вторая identity с тем же каталогом ждут."""
    profile_template: Path | None = None
    """Каталог-образец: копируется в `user_data_dir` перед первым запуском, если профиля ещё нет
    (каталога нет или он пуст). Только вместе с `user_data_dir`."""

    def __post_init__(self) -> None:
        check_choice(self, "mode", StateMode)
        for name in ("user_data_dir", "profile_template"):
            value = getattr(self, name)
            if isinstance(value, str):  # строка вместо `Path` не ломает открытие контекста
                object.__setattr__(self, name, Path(value))
        if self.profile_template is not None and self.user_data_dir is None:
            msg = "StatePolicy.profile_template требует user_data_dir: образец копируется в профиль"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextOptions:
    """Настройки контекста identity поверх настроек пула: локаль, часовой пояс, окно."""

    locale: str | None = None
    timezone: str | None = None
    """IANA-имя: `Europe/Moscow`."""
    geolocation: Geolocation | None = None
    viewport: Viewport | None = None
    user_agent: str | None = None
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    """Нативные опции SDK для контекста этой identity."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Identity:
    """От чьего имени работает браузер: ключ, вариант и политики."""

    key: str
    """Уникален в пуле: `"mail:42"`. Не секрет — попадает в логи и события."""
    variant: Hashable | None = None
    """Вариант сессии, который контекст не изолирует (через прокси или напрямую, имя в профиле).
    Смена варианта — пересоздание контекста, когда у identity нет занятых вкладок."""
    payload: Any = field(default=None, repr=False)
    """Креды и модель аккаунта — только для site SDK. В `repr` не попадает."""
    proxy: ProxyPolicy = field(default_factory=ProxyPolicy.pool)
    state: StatePolicy = field(default_factory=StatePolicy)
    flow: SessionFlow[Any, Any, Any] | None = field(default=None, repr=False)
    """Как открыть сессию этой identity; `None` — flow пула."""
    max_pages: int | None = None
    """Потолок вкладок этой identity; может только уменьшить потолок пула."""
    context_options: ContextOptions | None = None
    labels: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Метки для групповых потолков (`GroupLimit`) и снимков: `{"service": "mail"}`."""

    def __post_init__(self) -> None:
        if not self.key.strip():
            msg = "key identity не может быть пустым"
            raise ValueError(msg)
        try:
            hash(self.variant)
        except TypeError as error:
            msg = f"variant identity {self.key} должен быть хешируемым, получено {type(self.variant).__name__}"
            raise ValueError(msg) from error
        if self.max_pages is not None and self.max_pages < 1:
            msg = f"max_pages identity {self.key} должен быть ≥ 1, получено {self.max_pages}"
            raise ValueError(msg)
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))

    @override
    def __eq__(self, other: object) -> bool:
        # Идентичность — ключ и вариант; payload, flow и политики в сравнении не участвуют.
        if not isinstance(other, Identity):
            return NotImplemented
        return (self.key, self.variant) == (other.key, other.variant)

    @override
    def __hash__(self) -> int:
        # Payload может быть нехешируемым (словарь кредов); идентичность — ключ и вариант.
        return hash((self.key, self.variant))


__all__ = [
    "ContextOptions",
    "Identity",
    "ProxyMode",
    "ProxyPolicy",
    "StateMode",
    "StatePolicy",
]
