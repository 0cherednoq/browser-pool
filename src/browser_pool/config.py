"""Конфиг пула: сколько чего может существовать, кто сколько занимает, когда что закрывать.

Корень — `PoolConfig` из секций; у каждого поля дефолт, и пул поднимается вовсе без конфига.
Дефолты рассчитаны на долгоживущие залогиненные аккаунты; для одноразовых анонимных
identity есть пресет `PoolConfig.scraping()`.

Все длительности — секунды во `float`: это единица asyncio (`sleep`, `timeout`, `call_later`),
и доли секунды нужны (`spawn_delay=0.5`). `from_mapping` принимает ещё и `timedelta`.

Валидация — при создании: несовместимые значения падают `ConfigError` сразу, а не когда
пул до них дойдёт. `from_mapping` сверх того ловит неизвестные ключи и неверные типы — все
разом, с путём до поля и подсказкой ближайшего имени.
"""

from __future__ import annotations

import difflib
import math
import types
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields, is_dataclass, replace
from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NoReturn,
    Self,
    TypeAliasType,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from browser_pool._choice import check_choice
from browser_pool.errors import ConfigError
from browser_pool.geometry import Rect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from _typeshed import DataclassInstance


def _fail(owner: object, name: str, requirement: str) -> NoReturn:
    problem = f"{type(owner).__name__}.{name} {requirement}, получено {getattr(owner, name)!r}"
    raise ConfigError(problem)


def _at_least(owner: object, name: str, minimum: float) -> None:
    value = getattr(owner, name)
    if value is not None and value < minimum:
        _fail(owner, name, f"должно быть не меньше {minimum:g}")


def _positive(owner: object, *names: str) -> None:
    """Секунды: конечное положительное число (или `None`, где поле его допускает)."""
    for name in names:
        value = getattr(owner, name)
        if value is not None and not (math.isfinite(value) and value > 0):
            _fail(owner, name, "должно быть конечным положительным числом секунд")


def _non_negative(owner: object, *names: str) -> None:
    for name in names:
        value = getattr(owner, name)
        if value is not None and not (math.isfinite(value) and value >= 0):
            _fail(owner, name, "должно быть конечным неотрицательным числом")


@dataclass(frozen=True, slots=True, kw_only=True)
class Topology:
    """Физическая ёмкость: браузеры, контексты, вкладки. Ёмкость пула - `browsers × pages_per_browser`."""

    browsers: int = 2
    """Максимум процессов браузера. Главный регулятор памяти."""
    min_browsers: int = 0
    """Сколько держать запущенными всегда; остальные стартуют лениво по спросу."""
    pages_per_browser: int = 8
    """Максимум открытых вкладок в браузере: занятые и тёплые вместе."""
    contexts_per_browser: int = 12
    """Максимум контекстов в браузере; сверх него простаивающие вытесняются по давности."""
    pages_per_identity: int | None = None
    """Потолок вкладок одной identity по умолчанию; `Identity.max_pages` может только уменьшить."""
    warm_pages_per_identity: int = 1
    """Сколько простаивающих вкладок identity держать тёплыми после возврата."""

    def __post_init__(self) -> None:
        _at_least(self, "browsers", 1)
        _at_least(self, "min_browsers", 0)
        if self.min_browsers > self.browsers:
            _fail(self, "min_browsers", f"не может быть больше browsers={self.browsers}")
        _at_least(self, "pages_per_browser", 1)
        _at_least(self, "contexts_per_browser", 1)
        _at_least(self, "pages_per_identity", 1)
        _at_least(self, "warm_pages_per_identity", 0)
        ceiling = min(self.pages_per_browser, self.pages_per_identity or self.pages_per_browser)
        if self.warm_pages_per_identity > ceiling:
            _fail(
                self,
                "warm_pages_per_identity",
                f"не может быть больше вкладок, доступных identity ({ceiling})",
            )

    @property
    def capacity(self) -> int:
        """Сколько вкладок пул может держать открытыми одновременно."""
        return self.browsers * self.pages_per_browser


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupLimit:
    """Потолок на группу identity с меткой `label=value`: один сайт не съедает общий пул."""

    label: str
    """Имя метки identity (`Identity.labels`)."""
    value: str
    """Значение метки, к которому относится потолок."""
    max_pages: int | None = None
    """Сколько вкладок группа может занимать одновременно."""
    max_contexts: int | None = None
    """Сколько контекстов группы может жить одновременно."""

    def __post_init__(self) -> None:
        if not self.label:
            _fail(self, "label", "не может быть пустым")
        if self.max_pages is None and self.max_contexts is None:
            _fail(self, "max_pages", "или max_contexts должен быть задан — иначе это не потолок")
        _at_least(self, "max_pages", 1)
        _at_least(self, "max_contexts", 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits:
    """Параллельность и очередь: кто сколько может занять и сколько ждать."""

    max_waiting: int | None = None
    """Длина очереди ожидания; сверх неё - `PoolSaturatedError` сразу. `None` - без предела."""
    concurrent_launches: int = 1
    """Сколько браузеров стартует одновременно."""
    spawn_delay: float = 0.5
    """Пауза между стартами браузеров, секунды."""
    concurrent_opens: int = 4
    """Сколько открытий сессии (логинов) идёт одновременно во всём пуле."""
    concurrent_opens_per_proxy: int | None = 1
    """То же на один прокси. `None` - без предела."""
    max_identities_per_proxy: int | None = None
    """Сколько identity одновременно живёт на одном прокси. `None` - без предела."""
    groups: tuple[GroupLimit, ...] = ()
    """Потолки на группы identity по меткам."""
    leak_warn_after: float | None = 300.0
    """Аренда дольше - событие `LeakSuspected` со стеком места захвата. `None` - выключено."""
    lease_max_duration: float | None = None
    """Аренда дольше - принудительный отзыв: код внутри аренды прерывается, из `async with` вылетает
    `LeaseRevokedError`. `None` - не отзывать."""

    def __post_init__(self) -> None:
        _at_least(self, "max_waiting", 0)
        _non_negative(self, "spawn_delay")
        _positive(self, "leak_warn_after", "lease_max_duration")
        _at_least(self, "concurrent_launches", 1)
        _at_least(self, "concurrent_opens", 1)
        _at_least(self, "concurrent_opens_per_proxy", 1)
        _at_least(self, "max_identities_per_proxy", 1)
        keys = [(group.label, group.value) for group in self.groups]
        if len(keys) != len(set(keys)):
            _fail(self, "groups", "не должны повторять пару label=value")
        if (
            self.leak_warn_after is not None
            and self.lease_max_duration is not None
            and self.leak_warn_after >= self.lease_max_duration
        ):
            _fail(
                self, "leak_warn_after", "должно быть меньше lease_max_duration — иначе не успеет"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Backoff:
    """Паузы между попытками `pool.run`: растут от `initial` в `factor` раз до `maximum`.

    `jitter` - доля случайного разброса паузы в обе стороны (не выше `maximum`): задачи, упавшие
    разом, не повторяются тоже разом.
    """

    initial: float = 1.0
    """Пауза перед первым повтором, секунды."""
    maximum: float = 30.0
    """Потолок паузы, секунды."""
    factor: float = 2.0
    """Во сколько раз растёт пауза с каждой попыткой."""
    jitter: float = 0.1
    """Доля случайного разброса: 0.1 даёт 10% в обе стороны."""

    def __post_init__(self) -> None:
        _non_negative(self, "initial", "maximum")
        if self.maximum < self.initial:
            _fail(self, "maximum", f"не может быть меньше initial={self.initial}")
        if not (math.isfinite(self.factor) and self.factor >= 1):
            _fail(self, "factor", "должно быть конечным числом ≥ 1")
        if not 0 <= self.jitter <= 1:
            _fail(self, "jitter", "должно быть долей от 0 до 1")

    @classmethod
    def exp(cls, initial: float, maximum: float, *, jitter: float = 0.1) -> Self:
        """Экспонента: `initial`, ×2 с каждой попыткой, не больше `maximum`."""
        return cls(initial=initial, maximum=maximum, jitter=jitter)

    @classmethod
    def fixed(cls, seconds: float) -> Self:
        """Одна и та же пауза перед каждым повтором."""
        return cls(initial=seconds, maximum=seconds, factor=1.0, jitter=0.0)

    @classmethod
    def none(cls) -> Self:
        """Повторять сразу."""
        return cls(initial=0.0, maximum=0.0, factor=1.0, jitter=0.0)

    def delay(self, attempt: int, *, spread: float = 0.5) -> float:
        """Пауза перед повтором после попытки `attempt` (с нуля).

        `spread` — случайное число из [0, 1): 0.5 — без разброса. Пул передаёт своё.
        """
        base = min(self.initial * self.factor**attempt, self.maximum)
        return min(self.maximum, max(0.0, base * (1 + self.jitter * (2 * spread - 1))))


@dataclass(frozen=True, slots=True, kw_only=True)
class Lifecycle:
    """Простой и здоровье: когда закрывать простаивающее, как часто проверять и сохранять."""

    browser_idle_ttl: float | None = 300.0
    """Браузер без контекстов закрывается через столько секунд (не ниже `min_browsers`)."""
    context_idle_ttl: float | None = 600.0
    """Простаивающий контекст закрывается через столько секунд (состояние сохраняется)."""
    page_idle_ttl: float | None = 300.0
    """Простаивающая тёплая вкладка закрывается через столько секунд."""
    state_save_interval: float | None = 300.0
    """Периодическое сохранение состояния живых контекстов. `None` - только в ключевых точках."""
    healthcheck_interval: float = 15.0
    """Период проверки здоровья браузеров, секунды."""

    def __post_init__(self) -> None:
        _positive(
            self,
            "browser_idle_ttl",
            "context_idle_ttl",
            "page_idle_ttl",
            "state_save_interval",
            "healthcheck_interval",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Recycling:
    """Плановая смена браузеров и контекстов: по числу аренд, возрасту и счёту ошибок."""

    browser_max_leases: int | None = 500
    """Плановый перезапуск браузера после стольких аренд. `None` - выключено."""
    persistent_browser_max_leases: int | None = None
    """То же для браузера с профилем на диске (`StatePolicy.user_data_dir`). По умолчанию выключено:
    перезапуск не чистит профиль, а только прерывает работу аккаунта. `browser_max_age` действует."""
    browser_max_age: float | None = 3600.0
    """Плановый перезапуск браузера после стольких секунд жизни. `None` - выключено."""
    recycle_jitter: float = 0.1
    """Разброс порогов перезапуска (доля, меньше 0.5): браузеры не перезапускаются разом."""
    context_max_leases: int | None = None
    """Контекст на выход после стольких аренд. `None` - выключено (дефолт для аккаунтов)."""
    context_max_age: float | None = None
    """Контекст на выход после стольких секунд жизни. `None` - выключено."""
    context_max_error_score: float | None = None
    """Контекст на выход, когда счёт ошибок дорос до порога (+1 за сбой). `None` - выключено."""
    context_error_score_decrement: float = 0.5
    """На сколько успешная аренда уменьшает счёт ошибок контекста."""

    def __post_init__(self) -> None:
        _positive(self, "browser_max_age", "context_max_age", "context_max_error_score")
        _non_negative(self, "context_error_score_decrement")
        _at_least(self, "browser_max_leases", 1)
        _at_least(self, "persistent_browser_max_leases", 1)
        _at_least(self, "context_max_leases", 1)
        if not 0 <= self.recycle_jitter < 0.5:  # noqa: PLR2004 — граница из докстринга поля
            _fail(self, "recycle_jitter", "должно быть в [0, 0.5)")


def _doubling(initial: float, maximum: float) -> Backoff:
    """Пауза, которая удваивается до потолка, без разброса."""
    return Backoff(initial=initial, maximum=maximum, factor=2.0, jitter=0.0)


@dataclass(frozen=True, slots=True, kw_only=True)
class Recovery:
    """Самовосстановление: паузы и повторы после сбоев браузера, открытия сессии, прокси."""

    restart_max_attempts: int = 5
    """Сколько попыток подряд восстановить браузер после карантина. Не вышло - следующая серия через
    `restart_backoff.maximum`. `0` - не восстанавливать вовсе."""
    restart_backoff: Backoff = field(default_factory=lambda: _doubling(1.0, 60.0))
    """Паузы между попытками восстановления браузера; `maximum` - ещё и пауза между сериями попыток."""
    open_failure_backoff: Backoff = field(default_factory=lambda: _doubling(30.0, 900.0))
    """Паузы identity после неудачных открытий сессии подряд."""
    rate_limited_cooldown: float = 60.0
    """Пауза identity при `rate_limited` без `retry_after`, секунды."""
    proxy_retries: int = 2
    """Сколько раз переоткрыть контекст с другим прокси при сбое прокси."""

    def __post_init__(self) -> None:
        _positive(self, "rate_limited_cooldown")
        _at_least(self, "restart_max_attempts", 0)
        _at_least(self, "proxy_retries", 0)


@dataclass(frozen=True, slots=True, kw_only=True)
class Timeouts:
    """Сколько секунд ждать каждую операцию. Истёк - типизированная ошибка и реакция, не зависание."""

    startup: float = 30.0
    """Запуск или подключение браузера."""
    open: float = 120.0
    """Открытие сессии (`SessionFlow.open`: восстановление или логин)."""
    context_create: float = 15.0
    """Создание контекста драйвером."""
    page_create: float = 15.0
    """Создание вкладки драйвером."""
    prepare_page: float = 60.0
    """Прогрев новой вкладки (`SessionFlow.prepare_page`)."""
    reset_page: float = 10.0
    """Подготовка вкладки к возврату в пул (`SessionFlow.reset_page`)."""
    state_export: float = 10.0
    """Снятие состояния сессии с контекста; столько же - на запись его в хранилище."""
    close: float = 10.0
    """Штатное закрытие вкладки, контекста или браузера."""
    kill: float = 5.0
    """Жёсткое завершение браузера после неудачного закрытия."""
    restart: float = 60.0
    """Одна попытка восстановления браузера."""
    drain: float = 60.0
    """Ожидание занятых аренд при остановке пула."""
    ping: float = 5.0
    """Проверка живости браузера."""
    acquire: float | None = None
    """Дефолт для `pool.page(acquire_timeout=)`: сколько ждать выдачи. `None` - ждать, пока пул жив."""
    acquire_watchdog: float = 60.0
    """Раз в столько секунд ожидания выдачи снимок пула уходит в лог и событие; ожидание продолжается."""

    def __post_init__(self) -> None:
        _positive(self, *(item.name for item in fields(self)))


type WindowMode = Literal["off", "per_context", "per_page"]
type WindowLayout = Literal["grid", "columns", "rows", "cascade", "free"]
type WindowOverflow = Literal["minimize_idle", "cascade", "tabs"]
type WindowReflow = Literal["stable", "fill"]
type WindowOrder = Literal["browser", "identity", "created"]
type WindowGroup = Literal["none", "identity"]
type Screen = Literal["auto"] | int | Rect | tuple[Rect, ...]


_WINDOW_CHOICES = (
    ("mode", WindowMode),
    ("layout", WindowLayout),
    ("overflow", WindowOverflow),
    ("reflow", WindowReflow),
    ("order", WindowOrder),
    ("group", WindowGroup),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Windows:
    """Окна для отладки: каждый аккаунт или вкладка - в своём окне, окна рядом, без перекрытий.

    Только для headed-режима. Драйвер без `WindowControl` или headless - секция игнорируется с
    одним предупреждением, пул работает как обычно. Сбой оконной операции - только запись в лог.
    """

    mode: WindowMode = "off"
    """`per_context` - окно на аккаунт; `per_page` - окно на вкладку (нужен `new_window` драйвера)."""
    layout: WindowLayout = "grid"
    snap: bool = True
    """Окна встают в ячейки встык, с `gap`; `False` - задаётся только размер, позицию выбирает ОС."""
    size: tuple[int, int] | None = None
    """Фиксированный размер окна; `None` - размер ячейки."""
    min_size: tuple[int, int] = (560, 400)
    """Меньше ячейку не делать. Chrome не делает окно у́же ~534 px: меньший размер окна перекрыл бы соседей."""
    gap: int = 8
    margin: int = 0
    screen: Screen = "auto"
    """`auto` - рабочая область монитора первой вкладки; `Rect` или их кортеж - явно (мониторы по порядку)."""
    max_windows: int | None = None
    overflow: WindowOverflow = "minimize_idle"
    """Окон больше, чем ячеек: свернуть простаивающие, лесенкой или вкладками в существующих окнах."""
    reflow: WindowReflow = "stable"
    """`stable` - окно в своей ячейке всю жизнь; `fill` - пересчитывать сетку на каждое окно."""
    order: WindowOrder = "browser"
    group: WindowGroup = "none"
    """`identity` - окна одного аккаунта всегда рядом: у аккаунта свой блок ячеек подряд (размером с его
    потолок вкладок), сетка шириной в блок - аккаунт занимает строку. Нет свободного блока - окно
    сворачивается по `overflow`, но среди чужих не встаёт."""
    fit_viewport: bool = True
    """Viewport страницы следует за окном, а не остаётся 1280×720 в маленьком окне."""
    focus_on_acquire: bool = False
    respect_manual: bool = True
    """Окно, сдвинутое руками, не возвращается в ячейку до `retile()`."""

    def __post_init__(self) -> None:
        for name, alias in _WINDOW_CHOICES:
            check_choice(self, name, alias, error=ConfigError)
        if min(self.min_size) < 1:
            _fail(self, "min_size", "должен быть положительным")
        if self.size is not None and (
            self.size[0] < self.min_size[0] or self.size[1] < self.min_size[1]
        ):
            _fail(self, "size", f"не может быть меньше min_size {self.min_size}")
        _at_least(self, "gap", 0)
        _at_least(self, "margin", 0)
        _at_least(self, "max_windows", 1)
        if isinstance(self.screen, int) and not isinstance(self.screen, bool):
            _at_least(self, "screen", 0)
        if isinstance(self.screen, tuple) and not self.screen:
            _fail(self, "screen", "не может быть пустым списком")

    @property
    def active(self) -> bool:
        """Окна вообще раскладываются."""
        return self.mode != "off"


@dataclass(frozen=True, slots=True, kw_only=True)
class Debug:
    """Отладочные опции. Всё выключено по умолчанию."""

    label_windows: bool = False
    """Префикс `[mail:42 · proxy#7]` в заголовке окна - меняет страницу, только для отладки."""
    hold_on_error: float | None = None
    """При исключении в аренде вкладка не закрывается столько секунд - посмотреть глазами."""
    slow_mo: float | None = None
    """Замедление операций SDK, мс - если драйвер умеет."""
    keep_background_active: bool = False
    """Флаги Chromium, чтобы свёрнутые и перекрытые окна не замедлялись."""

    def __post_init__(self) -> None:
        _positive(self, "hold_on_error")
        _non_negative(self, "slow_mo")


type PressureAction = Literal["hold", "shrink"]
_PERCENT = 100.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Resources:
    """Защита хоста: не расти под давлением памяти и CPU, лечить распухшие браузеры.

    Замеры - `HostProbe` (psutil из extra `[resources]`); без него секция игнорируется с
    предупреждением. Всё выключено по умолчанию.
    """

    max_browser_rss_mb: float | None = None
    """Дерево процессов браузера толще - внеплановый перезапуск с дренажом, не убийство."""
    min_free_memory_mb: float | None = None
    """Свободной памяти хоста меньше - не запускать новые браузеры и контексты."""
    max_cpu_percent: float | None = None
    """Средняя загрузка CPU за `sample_window` выше - то же."""
    sample_window: float = 10.0
    """За сколько секунд усредняется загрузка CPU."""
    pressure_action: PressureAction = "hold"
    """`hold` - только придержать рост; `shrink` - ещё и закрывать простаивающее до выхода из-под давления."""

    def __post_init__(self) -> None:
        check_choice(self, "pressure_action", PressureAction, error=ConfigError)
        _positive(self, "max_browser_rss_mb", "min_free_memory_mb", "sample_window")
        if self.max_cpu_percent is not None and not 0 < self.max_cpu_percent <= _PERCENT:
            _fail(self, "max_cpu_percent", "должно быть в (0, 100]")

    @property
    def active(self) -> bool:
        """Задан хоть один порог."""
        return any(
            limit is not None
            for limit in (self.max_browser_rss_mb, self.min_free_memory_mb, self.max_cpu_percent)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PoolConfig:
    """Конфиг пула целиком. Каждая секция необязательна: пропущенная берёт дефолты."""

    topology: Topology = field(default_factory=Topology)
    """Сколько чего может существовать."""
    limits: Limits = field(default_factory=Limits)
    """Кто сколько может занять и сколько ждать."""
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    """Когда закрывать простаивающее и как часто проверять."""
    recycling: Recycling = field(default_factory=Recycling)
    """Плановая смена браузеров и контекстов."""
    recovery: Recovery = field(default_factory=Recovery)
    """Паузы и повторы после сбоев."""
    timeouts: Timeouts = field(default_factory=Timeouts)
    """Сколько ждать каждую операцию."""
    windows: Windows = field(default_factory=Windows)
    """Окна для отладки: раскладка без перекрытий."""
    debug: Debug = field(default_factory=Debug)
    """Отладочные опции."""
    resources: Resources = field(default_factory=Resources)
    """Защита хоста: память и CPU."""

    @classmethod
    def accounts(cls) -> Self:
        """Пресет для долгоживущих залогиненных аккаунтов.

        Вход дорог — контексты живут долго, вкладки держатся тёплыми, счётчики одноразовых
        сессий выключены, логины с одного прокси не идут разом.
        """
        return cls(lifecycle=Lifecycle(context_idle_ttl=1800.0))

    @classmethod
    def scraping(cls) -> Self:
        """Пресет для анонимных одноразовых identity: без тёплых вкладок, со счётчиками сессий."""
        return cls(
            topology=Topology(warm_pages_per_identity=0),
            limits=Limits(concurrent_opens=16, concurrent_opens_per_proxy=None),
            lifecycle=Lifecycle(context_idle_ttl=30.0, page_idle_ttl=30.0),
            recycling=Recycling(
                context_max_leases=50, context_max_age=3000.0, context_max_error_score=3.0
            ),
        )

    def replace(
        self,
        *,
        topology: Topology | Mapping[str, object] | None = None,
        limits: Limits | Mapping[str, object] | None = None,
        lifecycle: Lifecycle | Mapping[str, object] | None = None,
        recycling: Recycling | Mapping[str, object] | None = None,
        recovery: Recovery | Mapping[str, object] | None = None,
        timeouts: Timeouts | Mapping[str, object] | None = None,
        windows: Windows | Mapping[str, object] | None = None,
        debug: Debug | Mapping[str, object] | None = None,
        resources: Resources | Mapping[str, object] | None = None,
    ) -> Self:
        """Производный конфиг: переданные секции заменяются, остальные остаются как есть.

        Секция целиком (`limits=Limits(...)`) заменяет секцию — поля пресета, которых в ней нет,
        вернутся к умолчаниям. Словарь (`limits={"max_waiting": 50}`) меняет только названные поля,
        остальные остаются от текущей секции; неизвестное имя — `ConfigError` с подсказкой.
        """
        given: dict[str, object] = {
            "topology": topology,
            "limits": limits,
            "lifecycle": lifecycle,
            "recycling": recycling,
            "recovery": recovery,
            "timeouts": timeouts,
            "windows": windows,
            "debug": debug,
            "resources": resources,
        }
        changes: dict[str, object] = {}
        for name, value in given.items():
            if value is None:
                continue
            changes[name] = (
                _merged(name, getattr(self, name), cast("Mapping[str, object]", value))
                if isinstance(value, Mapping)
                else value
            )
        return replace(self, **changes)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        """Конфиг из словаря — TOML, YAML, env, настроек приложения.

        Пропущенный ключ — дефолт; неизвестный ключ и неверный тип — `ConfigError` со всеми
        проблемами разом. Секунды можно задать и `timedelta`.
        """
        parser = _Parser()
        try:
            built = parser.build(cls, raw, "")
        except _Invalid:
            raise ConfigError(*parser.problems) from None
        return cast("Self", built)

    def to_mapping(self) -> dict[str, Any]:
        """Конфиг как простые данные: словари, списки, числа, строки, `None`."""
        return _dump_dataclass(self)


# --- разбор словаря по аннотациям полей --------------------------------------------------
# Разбор не знает конкретных секций: новая секция конфига — это новое поле, и только.


class _Invalid(Exception):  # noqa: N818 — внутренний маркер «поле не разобралось», наружу не выходит
    """Значение поля не разобралось; проблема уже записана."""


_NOT_CONVERTED = object()
_EXPECTED: dict[object, str] = {
    bool: "true или false",
    float: "число секунд или timedelta",
    int: "целое число",
    str: "строка",
}


def _convert_scalar(target: object, raw: object) -> object:
    """Значение поля простого типа или `_NOT_CONVERTED`. `bool` не число: `browsers=True` — ошибка."""
    if isinstance(raw, bool):
        return raw if target is bool else _NOT_CONVERTED
    if target is float and isinstance(raw, timedelta):
        return raw.total_seconds()
    if target is float and isinstance(raw, int | float):
        return float(raw)
    if target is int and isinstance(raw, int):
        return raw
    if target is str and isinstance(raw, str):
        return raw
    return _NOT_CONVERTED


class _Parser:
    """Собирает dataclass-конфиг из словаря, копя все проблемы, а не останавливаясь на первой."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def build(self, target: object, raw: object, path: str) -> object:
        if isinstance(target, TypeAliasType):
            return self.build(target.__value__, raw, path)
        if isinstance(target, type) and is_dataclass(target):
            return self._dataclass(target, raw, path)
        origin = get_origin(target)
        if origin in {Union, types.UnionType}:
            return self._union(get_args(target), raw, path)
        if origin is tuple:
            return self._tuple(get_args(target), raw, path)
        if origin is Literal:
            if raw in get_args(target):
                return raw
            self._reject(path, f"одно из {get_args(target)!r}", raw)
        if target not in _EXPECTED:
            msg = f"Поле {path}: аннотация {target!r} не поддерживается разбором конфига"
            raise TypeError(msg)
        scalar = cast("object", target)
        converted = _convert_scalar(scalar, raw)
        if converted is _NOT_CONVERTED:
            self._reject(path, _EXPECTED[scalar], raw)
        return converted

    def _dataclass(self, target: type, raw: object, path: str) -> object:
        if not isinstance(raw, Mapping):
            self._reject(path or "конфиг", "словарь", raw)
        given = cast("Mapping[object, object]", raw)
        known_problems = len(self.problems)  # проблемы соседних секций не мешают проверить эту
        self._check_keys(target, given, path)
        hints = get_type_hints(target)
        values: dict[str, object] = {}
        for name, value in given.items():
            if name in hints:
                try:
                    values[str(name)] = self.build(hints[str(name)], value, _join(path, str(name)))
                except _Invalid:
                    continue
        if len(self.problems) > known_problems:
            raise _Invalid
        try:
            return cast("Callable[..., object]", target)(**values)
        except ConfigError as error:
            self.problems.extend(error.problems)
            raise _Invalid from error
        except ValueError as error:
            self.problems.append(f"{path or 'конфиг'}: {error}")
            raise _Invalid from error

    def _union(self, options: tuple[object, ...], raw: object, path: str) -> object:
        """Первый вариант, в который значение разбирается; `None` — если он допустим."""
        if raw is None and type(None) in options:
            return None
        variants = [option for option in options if option is not type(None)]
        if len(variants) == 1:
            return self.build(variants[0], raw, path)
        for variant in variants:
            trial = _Parser()
            try:
                return trial.build(variant, raw, path)
            except _Invalid:
                continue
        return self._reject(path, "одно из допустимых значений", raw)

    def _check_keys(self, target: type, given: Mapping[object, object], path: str) -> None:
        """Неизвестные ключи — с подсказкой ближайшего имени; отсутствующие обязательные."""
        members = [item for item in fields(target) if item.init]
        known = [item.name for item in members]
        self.problems.extend(
            _unknown_key(_join(path, str(key)), str(key), known)
            for key in given
            if key not in known
        )
        self.problems.extend(
            f"{_join(path, item.name)}: обязательный ключ отсутствует"
            for item in members
            if item.name not in given
            and item.default is MISSING
            and item.default_factory is MISSING
        )

    def _tuple(self, items: tuple[object, ...], raw: object, path: str) -> tuple[object, ...]:
        """`tuple[X, ...]` — список любой длины; `tuple[X, Y]` — ровно столько элементов."""
        if isinstance(raw, str | bytes | Mapping) or not isinstance(raw, list | tuple):
            self._reject(path, "список", cast("object", raw))
        elements = cast("Sequence[object]", raw)
        homogeneous = len(items) == 2 and items[1] is Ellipsis  # noqa: PLR2004 — tuple[X, ...]
        if not homogeneous and len(elements) != len(items):
            self._reject(path, f"список из {len(items)} элементов", cast("object", raw))
        built: list[object] = []
        for index, element in enumerate(elements):
            item = items[0] if homogeneous else items[index]
            try:
                built.append(self.build(item, element, f"{path}[{index}]"))
            except _Invalid:
                continue
        if len(built) != len(elements):
            raise _Invalid
        return tuple(built)

    def _reject(self, path: str, expected: str, raw: object) -> NoReturn:
        self.problems.append(f"{path}: ожидается {expected}, получено {raw!r}")
        raise _Invalid


def _merged(section_name: str, current: object, overrides: Mapping[str, object]) -> object:
    """Секция `current` с полями из `overrides`; неизвестное имя поля — `ConfigError`."""
    known = [item.name for item in fields(cast("DataclassInstance", current))]
    problems = [
        _unknown_key(f"{section_name}.{key}", key, known) for key in overrides if key not in known
    ]
    if problems:
        raise ConfigError(*problems)
    return replace(cast("DataclassInstance", current), **overrides)


def _unknown_key(path: str, key: str, known: Iterable[str]) -> str:
    close = difflib.get_close_matches(key, list(known), n=1)
    hint = f" — может быть, {close[0]}?" if close else ""
    return f"{path}: неизвестный ключ{hint}"


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _dump_dataclass(value: DataclassInstance) -> dict[str, Any]:
    return {item.name: _dump(getattr(value, item.name)) for item in fields(value)}


def _dump(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _dump_dataclass(value)
    if isinstance(value, tuple):
        return [_dump(element) for element in cast("tuple[object, ...]", value)]
    return value


__all__ = [
    "Backoff",
    "Debug",
    "GroupLimit",
    "Lifecycle",
    "Limits",
    "PoolConfig",
    "PressureAction",
    "Recovery",
    "Recycling",
    "Resources",
    "Screen",
    "Timeouts",
    "Topology",
    "WindowGroup",
    "WindowLayout",
    "WindowMode",
    "WindowOrder",
    "WindowOverflow",
    "WindowReflow",
    "Windows",
]
