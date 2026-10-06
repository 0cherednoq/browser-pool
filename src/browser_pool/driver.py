"""Контракт драйвера: минимум, который пул просит у браузерного SDK.

Здесь нет ни одного SDK. Драйвер — адаптер в `browser_pool.drivers` — переводит эти вызовы
на Playwright, Camoufox, pydoll, Selenium. Всё, чего нет в протоколе, пул у SDK не просит, а
то, в чём SDK расходятся, драйвер объявляет возможностями (`DriverCapabilities`): пул решает
по ним, а не по `isinstance` на конкретный SDK.

Браузер, контекст и вкладка для пула непрозрачны (`B`, `C`, `P`): арендатор получает их
нативными объектами SDK, без обёрток.

Писать драйвер удобнее от `BaseDriver`: обязательны только запуск, проверка живости, контекст и
вкладка с их закрытием; остальное по умолчанию честно отвечает «не умею». Драйвер, который объявил
возможность и не реализовал её, пул не примет: `BrowserPool(...)` падает `ConfigError` со всеми
расхождениями разом (`driver_problems`), а не деградирует молча посреди работы.

`LaunchSpec` и `ContextSpec` изменяемы намеренно: хуки `before_launch` и `before_context` дописывают
в них своё (отпечаток, локаль, нативные опции SDK через `extra`). Каждый запуск получает
свою спецификацию, так что правки не утекают между вызовами.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from browser_pool._choice import check_choice
from browser_pool.errors import ConfigError, UnsupportedRequirementError
from browser_pool.proxies import SCHEMES
from browser_pool.state import SessionState

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from browser_pool.errors import ErrorKind
    from browser_pool.geometry import Geolocation, Rect, Viewport
    from browser_pool.proxies import Proxy
    from browser_pool.state import Cookie


# --- геометрия и окна ------------------------------------------------------------------


class WindowState(StrEnum):
    """Состояние окна браузера."""

    normal = "normal"
    minimized = "minimized"
    maximized = "maximized"
    fullscreen = "fullscreen"


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowBounds:
    """Где окно и в каком оно состоянии."""

    rect: Rect
    state: WindowState = WindowState.normal


type WindowId = int | str
"""Идентификатор окна в терминах SDK: `windowId` CDP, handle WebDriver."""


@runtime_checkable
class WindowControl[B, P](Protocol):
    """Необязательная часть драйвера: двигать окна на лету. Только для отладки.

    Драйвер, который этого не умеет, объявляет `window_control="launch_only"` (окно задаётся
    при запуске, `LaunchSpec.window`) или `"none"`.
    """

    async def window_of(self, page: P) -> WindowId:
        """Окно, в котором показана вкладка."""
        ...

    async def get_bounds(self, browser: B, window: WindowId) -> WindowBounds:
        """Где окно сейчас."""
        ...

    async def set_bounds(self, browser: B, window: WindowId, bounds: WindowBounds) -> None:
        """Поставить окно на место."""
        ...

    async def screen_area(self, page: P) -> Rect:
        """Рабочая область монитора, на котором стоит окно вкладки."""
        ...

    async def bring_to_front(self, page: P) -> None:
        """Поднять окно вкладки поверх остальных."""
        ...


@runtime_checkable
class PageLabeler[P](Protocol):
    """Необязательная часть драйвера: подписать окно вкладки (`Debug.label_windows`).

    Подпись — префикс заголовка страницы; он переживает навигацию и смену заголовка сайтом.
    Меняет страницу, поэтому только для отладки.
    """

    async def label_page(self, page: P, label: str) -> None:
        """Поставить префикс `label` в заголовок вкладки."""
        ...


# --- возможности -----------------------------------------------------------------------

type ProxyScope = Literal["context", "browser", "external"]
type FingerprintScope = Literal["context", "browser", "external", "none"]
type StateSupport = Literal["full", "cookies", "none"]
type WindowControlLevel = Literal["runtime", "launch_only", "none"]

CONTEXT_SETTINGS: frozenset[str] = frozenset(
    {"locale", "timezone", "geolocation", "viewport", "user_agent"}
)
"""Настройки контекста identity (`ContextOptions`), которые драйвер может уметь применять."""
DEBUG_OPTIONS: frozenset[str] = frozenset({"slow_mo", "keep_background_active"})
"""Отладочные опции запуска (`Debug`), которые драйвер может уметь применять."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DriverCapabilities:
    """Что драйвер умеет по жизненному циклу. Не объявлено — значит не умеет.

    Только жизненный цикл: браузер, контекст, прокси, состояние, окна. Что SDK умеет на
    странице (сеть, shadow DOM), — дело site SDK.
    """

    proxy_scope: ProxyScope
    """На чём висит прокси: на контексте (много identity в браузере), на браузере или у вендора."""
    can_new_context: bool = False
    """Может ли создать новый контекст. Антидетект-профиль — нет: только готовый."""
    fingerprint_scope: FingerprintScope = "none"
    """На чём висит отпечаток."""
    state_support: StateSupport = "none"
    """Что из состояния сессии драйвер выгружает и загружает: всё, только куки или ничего."""
    persistent_dir: bool = False
    """Умеет ли профиль на диске (`user_data_dir`)."""
    proxy_auth: bool = False
    """Умеет ли SDK сам авторизоваться на прокси логином и паролем."""
    proxy_schemes: frozenset[str] = frozenset({"http"})
    """Схемы прокси, которые понимает SDK."""
    proxy_auth_schemes: frozenset[str] | None = None
    """Схемы, на которых SDK умеет авторизацию; `None` — на всех `proxy_schemes`. Chromium, например,
    не авторизуется на socks: прокси `socks5` с логином такой драйвер не поднимет."""
    thread_affinity: bool = False
    """Экспериментально (драйвера с этой возможностью в поставке нет). SDK синхронный: у каждого браузера свой поток, все асинхронные вызовы драйвера по нему —
    оттуда (`lease.call` — тоже). Синхронные методы (`pid`, `page_usable`, `on_disconnect`,
    `classify`) пул зовёт из своего потока — драйвер отвечает на них, не обращаясь к SDK."""
    new_window: bool = False
    """Может открыть вкладку в отдельном окне того же браузера."""
    window_control: WindowControlLevel = "none"
    """Двигает окна на лету, задаёт только при запуске или никак."""
    max_pages_hint: int | None = None
    """Сколько вкладок на браузер SDK выдерживает; меньше конфига — берётся меньшее."""
    context_settings: frozenset[str] = frozenset()
    """Какие настройки контекста identity драйвер применяет к новому контексту: имена из
    `CONTEXT_SETTINGS` (`locale`, `timezone`, `geolocation`, `viewport`, `user_agent`). Identity,
    которой нужна необъявленная настройка, пул не обслужит — а не выдаст ей контекст без неё."""
    debug_options: frozenset[str] = frozenset()
    """Какие отладочные опции запуска драйвер применяет: имена из `DEBUG_OPTIONS`. Опция конфига,
    которой драйвер не умеет, — предупреждение при старте пула."""

    def __post_init__(self) -> None:
        for name, alias in (
            ("proxy_scope", ProxyScope),
            ("fingerprint_scope", FingerprintScope),
            ("state_support", StateSupport),
            ("window_control", WindowControlLevel),
        ):
            check_choice(self, name, alias, error=ConfigError)
        for problem in (*self._proxy_problems(), *self._other_problems()):
            raise ConfigError(problem)

    def _proxy_problems(self) -> list[str]:
        problems: list[str] = []
        if self.proxy_scope == "context" and not self.can_new_context:
            problems.append(
                "DriverCapabilities: proxy_scope='context' требует can_new_context=True"
            )
        unknown = self.proxy_schemes - SCHEMES
        if unknown:
            problems.append(f"DriverCapabilities: неизвестные схемы прокси {sorted(unknown)}")
        if self.proxy_auth_schemes is not None:
            if not self.proxy_auth:
                problems.append(
                    "DriverCapabilities: proxy_auth_schemes без proxy_auth — авторизации нет вовсе"
                )
            foreign = self.proxy_auth_schemes - self.proxy_schemes
            if foreign:
                problems.append(
                    f"DriverCapabilities: proxy_auth_schemes вне proxy_schemes: {sorted(foreign)}"
                )
        return problems

    def _other_problems(self) -> list[str]:
        problems: list[str] = []
        if self.new_window and self.window_control == "none":
            problems.append(
                "DriverCapabilities: new_window без window_control — окно некому разместить"
            )
        for name, known in (
            ("context_settings", CONTEXT_SETTINGS),
            ("debug_options", DEBUG_OPTIONS),
        ):
            unknown = getattr(self, name) - known
            if unknown:
                problems.append(
                    f"DriverCapabilities: неизвестные {name} {sorted(unknown)}; "
                    f"допустимы {sorted(known)}"
                )
        if self.max_pages_hint is not None and self.max_pages_hint < 1:
            problems.append(
                f"DriverCapabilities: max_pages_hint должен быть ≥ 1, получено {self.max_pages_hint}"
            )
        return problems


# --- спецификации ----------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class LaunchSpec:
    """Что нужно для запуска браузера. Хуки `before_launch` дописывают сюда своё."""

    headless: bool = True
    proxy: Proxy | None = None
    """Прокси на весь браузер — у драйверов с `proxy_scope="browser"`."""
    user_data_dir: Path | None = None
    """Профиль на диске — у драйверов с `persistent_dir`."""
    window: Rect | None = None
    """Где открыть окно — у драйверов с `window_control="launch_only"`."""
    slow_mo: float | None = None
    """Замедлить каждую операцию SDK на столько миллисекунд — если драйвер умеет (отладка)."""
    keep_background_active: bool = False
    """Свёрнутые и перекрытые окна не замедляются — флаги браузера, если драйвер их знает."""
    args: list[str] = field(default_factory=list[str])
    """Аргументы командной строки браузера."""
    extra: dict[str, Any] = field(default_factory=dict[str, Any])
    """Нативные опции SDK — сквозь ядро, без его участия."""


@dataclass(slots=True, kw_only=True)
class ContextSpec:
    """Что нужно для контекста identity. Хуки `before_context` дописывают сюда своё."""

    proxy: Proxy | None = None
    """Прокси контекста — у драйверов с `proxy_scope="context"`."""
    state: SessionState | None = None
    """Состояние сессии: задаётся при создании, а не дозаливается в живой контекст."""
    locale: str | None = None
    timezone: str | None = None
    """IANA-имя: `Europe/Moscow`."""
    geolocation: Geolocation | None = None
    viewport: Viewport | None = None
    user_agent: str | None = None
    default_timeout: float | None = None
    """Таймаут операций SDK по умолчанию, секунды."""
    fit_window: bool = False
    """Страница следует за размером окна (окна для отладки), если `viewport` не задан явно."""
    window_per_page: bool = False
    """Каждая вкладка контекста — в своём окне (окна для отладки, `per_page`), а не вкладкой в
    окне контекста. Только у драйверов с `new_window`."""
    reuse_default: bool = False
    """Не создавать контекст, а взять готовый контекст браузера — профиль вендора (антидетект,
    `ProfileProvider`). Прокси, отпечаток и настройки у него свои: `proxy`, `locale` и прочее
    к нему не применяются; куки из `state` добавляются. Такой контекст драйвер не закрывает."""
    extra: dict[str, Any] = field(default_factory=dict[str, Any])
    """Нативные опции SDK — сквозь ядро, без его участия."""


type EndpointKind = Literal["cdp", "playwright_ws", "webdriver"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Endpoint:
    """Куда подключиться к уже запущенному браузеру: удалённый CDP, антидетект, облако."""

    kind: EndpointKind
    url: str = field(repr=False)
    """Адрес подключения. Часто несёт токен — в логи не выводить."""
    pid: int | None = None
    """Процесс браузера, если он локальный: пул добьёт его, если закрытие не удалось."""
    driver_path: Path | None = None
    """Свой бинарь драйвера (chromedriver антидетекта) для WebDriver."""

    def __post_init__(self) -> None:
        check_choice(self, "kind", EndpointKind)
        if not self.url:
            msg = "url эндпоинта не может быть пустым"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Evidence:
    """Что было на вкладке в момент сбоя. Снимается по возможности: пустое — тоже ответ."""

    screenshot: bytes | None = field(default=None, repr=False)
    html: str | None = field(default=None, repr=False)
    """Разметка страницы — может содержать данные пользователя; в логи не выводить."""
    url: str | None = None


# --- протокол --------------------------------------------------------------------------


@runtime_checkable
class Driver[B, C, P](Protocol):
    """Адаптер браузерного SDK. `B`, `C`, `P` — браузер, контекст и вкладка этого SDK.

    Каждый вызов пул ограничивает своими таймаутами (`Timeouts`); драйверу ждать самому не нужно.
    Но это значит, что любой вызов могут отменить посередине: метод, который что-то создаёт
    (`launch`, `attach`, `new_context`, `new_page`), при отмене и при своём сбое прибирает
    созданное сам — у пула нет ручки на полусозданный браузер или вкладку.
    """

    @property
    def capabilities(self) -> DriverCapabilities:
        """Что драйвер умеет по жизненному циклу."""
        ...

    async def prepare(self) -> None:
        """Перед работой пула: скачать или пропатчить бинарь, поднять процесс SDK.

        Зовётся на каждый пул, а не один раз на процесс: один драйвер может обслуживать несколько
        пулов. Каждый зовёт `prepare` при старте и `shutdown` при остановке — драйвер считает
        пользователей и освобождает общее после последнего `shutdown`.
        """
        ...

    async def shutdown(self) -> None:
        """Пул остановлен и всё закрыл: освободить то, что поднял `prepare`, — если этот пул последний."""
        ...

    async def launch(self, spec: LaunchSpec) -> B:
        """Запустить браузер."""
        ...

    async def attach(self, endpoint: Endpoint) -> B:
        """Подключиться к уже запущенному браузеру.

        Вид подключения, которого драйвер не умеет (`endpoint.kind`), — `UnsupportedRequirementError`.
        Чужой браузер драйвер не убивает: `close_browser` закрывает только соединение.
        """
        ...

    async def ping(self, browser: B) -> bool:
        """Дешёвая проверка живости: соединение есть и браузер отвечает.

        `False` и исключение значат одно — не отвечает: пул отправит браузер в карантин.
        """
        ...

    def on_disconnect(self, browser: B, callback: Callable[[], None]) -> None:
        """Позвать `callback`, когда браузер отвалится сам.

        Ускоряет реакцию, но не обязателен: SDK без такого события может не звать его вовсе —
        тогда падение найдёт `ping`. Звать и при своём закрытии можно: пул отличит.
        """
        ...

    async def close_browser(self, browser: B) -> None:
        """Штатно закрыть браузер. Уже умерший — тоже без исключения: закрывать нечего."""
        ...

    async def kill_browser(self, browser: B) -> None:
        """Жёстко завершить браузер: штатное закрытие не уложилось в таймаут."""
        ...

    def pid(self, browser: B) -> int | None:
        """Процесс браузера, если он локальный."""
        ...

    async def new_context(self, browser: B, spec: ContextSpec) -> C:
        """Создать контекст (или отдать единственный — у драйверов без `can_new_context`).

        Из `spec` применяется всё, что драйвер объявил в возможностях; `spec.reuse_default` —
        отдать готовый контекст браузера, не создавая.
        """
        ...

    async def close_context(self, context: C) -> None:
        """Закрыть контекст и его вкладки. Повторно и у умершего браузера — без исключения.

        Готовый контекст (`reuse_default`) драйвер не закрывает: он не пула.
        """
        ...

    async def export_state(self, context: C) -> SessionState:
        """Снять состояние сессии с контекста.

        `extras` возвращаются те, с которыми контекст создан (`spec.state.extras`): пул заменяет
        запись identity снятым состоянием целиком, и потерянные `extras` — потерянные токены
        site SDK. Кука, которую `Cookie` не представит, пропускается, а не роняет снятие.
        """
        ...

    async def add_cookies(self, context: C, cookies: Sequence[Cookie]) -> None:
        """Добавить куки в живой контекст.

        Пул сам его не зовёт; им пользуются драйвер (куки состояния — в готовый контекст
        `reuse_default`) и код приложения, которому нужно дозалить куки в живую сессию.
        """
        ...

    async def new_page(self, context: C) -> P:
        """Открыть вкладку."""
        ...

    def page_usable(self, page: P) -> bool:
        """Вкладка ещё жива: не закрыта — ни драйвером, ни сайтом, ни вместе с контекстом — и браузер жив.

        Синхронно и без обращения к SDK по сети: пул спрашивает перед каждой выдачей тёплой вкладки.
        """
        ...

    async def close_page(self, page: P) -> None:
        """Закрыть вкладку. Уже закрытую и вкладку умершего браузера — без исключения."""
        ...

    async def capture(self, page: P) -> Evidence:
        """Снимок и разметка вкладки — по возможности, без исключений."""
        ...

    def classify(self, error: BaseException) -> ErrorKind | None:
        """Нативная ошибка SDK → вид сбоя. `None` — драйвер не знает, что это.

        Чужую ошибку драйвер не присваивает. Ошибка операции на умершем браузере — `browser`
        (карантин браузера), на закрытой вкладке — `page`, отказ или недоступность прокси — `proxy`.
        """
        ...


class BaseDriver[B, C, P](ABC):
    """Основа драйвера: обязателен минимум, остальное по умолчанию честно «не умеет».

    Обязательны: `capabilities`, `launch`, `ping`, `close_browser`, `new_context`,
    `close_context`, `new_page`, `page_usable`, `close_page`. Умолчания остального согласованы
    с возможностями, которые не объявлены: без `state_support` состояние пустое, без события
    обрыва падение находит `ping`, без `pid` зависший браузер закрывается штатно ещё раз.
    Объявили возможность — переопределите метод: иначе пул откажется от драйвера при создании.
    """

    @property
    @abstractmethod
    def capabilities(self) -> DriverCapabilities:
        """Что драйвер умеет по жизненному циклу."""

    async def prepare(self) -> None:  # noqa: B027 — умолчание, а не забытый abstractmethod
        """По умолчанию готовить нечего."""

    async def shutdown(self) -> None:  # noqa: B027 — умолчание, а не забытый abstractmethod
        """По умолчанию освобождать нечего."""

    @abstractmethod
    async def launch(self, spec: LaunchSpec) -> B:
        """Запустить браузер."""

    async def attach(self, endpoint: Endpoint) -> B:
        """По умолчанию драйвер подключаться не умеет."""
        raise UnsupportedRequirementError(
            missing=(f"подключение к запущенному браузеру ({endpoint.kind})",)
        )

    @abstractmethod
    async def ping(self, browser: B) -> bool:
        """Дешёвая проверка живости: соединение есть и браузер отвечает."""

    def on_disconnect(self, browser: B, callback: Callable[[], None]) -> None:
        """По умолчанию события обрыва нет: падение браузера найдёт `ping`."""
        _ = browser, callback

    @abstractmethod
    async def close_browser(self, browser: B) -> None:
        """Штатно закрыть браузер."""

    async def kill_browser(self, browser: B) -> None:
        """По умолчанию жёстче штатного закрытия ничего нет: ещё раз `close_browser`."""
        await self.close_browser(browser)

    def pid(self, browser: B) -> int | None:
        """По умолчанию процесс браузера неизвестен."""
        _ = browser
        return None

    @abstractmethod
    async def new_context(self, browser: B, spec: ContextSpec) -> C:
        """Создать контекст (или отдать единственный — у драйверов без `can_new_context`)."""

    @abstractmethod
    async def close_context(self, context: C) -> None:
        """Закрыть контекст."""

    async def export_state(self, context: C) -> SessionState:
        """По умолчанию состояния нет (`state_support="none"`): пустое."""
        _ = context
        return SessionState()

    async def add_cookies(self, context: C, cookies: Sequence[Cookie]) -> None:
        """По умолчанию драйвер куки добавлять не умеет."""
        _ = context, cookies
        raise UnsupportedRequirementError(missing=("добавление кук в живой контекст",))

    @abstractmethod
    async def new_page(self, context: C) -> P:
        """Открыть вкладку."""

    @abstractmethod
    def page_usable(self, page: P) -> bool:
        """Вкладка ещё жива: не закрыта и контекст на месте."""

    @abstractmethod
    async def close_page(self, page: P) -> None:
        """Закрыть вкладку."""

    async def capture(self, page: P) -> Evidence:
        """По умолчанию улик нет: пустые."""
        _ = page
        return Evidence()

    def classify(self, error: BaseException) -> ErrorKind | None:
        """По умолчанию драйвер своих ошибок не знает."""
        _ = error
        return None


_PROTOCOL = (
    "capabilities",
    "prepare",
    "shutdown",
    "launch",
    "attach",
    "ping",
    "on_disconnect",
    "close_browser",
    "kill_browser",
    "pid",
    "new_context",
    "close_context",
    "export_state",
    "add_cookies",
    "new_page",
    "page_usable",
    "close_page",
    "capture",
    "classify",
)
_WINDOW_METHODS = ("window_of", "get_bounds", "set_bounds", "screen_area", "bring_to_front")


def driver_problems(driver: object, *, attaches: bool = False) -> tuple[str, ...]:
    """Чем драйвер расходится со своими возможностями и протоколом; пусто — годен.

    Проверяется то, что видно без запуска: все ли методы протокола есть; объявленное управление
    окнами — есть ли его методы; у наследника `BaseDriver` — переопределено ли то, что объявлено
    (`state_support` → `export_state`; пул с провайдером, `attaches=True`, → `attach`). Обратное
    не ошибка: возможности вправе быть уже реализации — не объявлено, значит пул не пользуется.
    """
    name = type(driver).__name__
    missing = [method for method in _PROTOCOL if not hasattr(driver, method)]
    if missing:
        return (f"{name}: нет методов протокола Driver: {', '.join(missing)}",)
    capabilities = getattr(driver, "capabilities", None)
    if not isinstance(capabilities, DriverCapabilities):
        return (
            f"{name}.capabilities должно быть DriverCapabilities, а не {type(capabilities).__name__}",
        )
    problems = _window_problems(driver, capabilities)
    if isinstance(driver, BaseDriver):
        base = cast("BaseDriver[Any, Any, Any]", driver)
        problems += _default_problems(base, capabilities, attaches=attaches)
    return tuple(problems)


def _window_problems(driver: object, capabilities: DriverCapabilities) -> list[str]:
    if capabilities.window_control != "runtime":
        return []
    absent = [method for method in _WINDOW_METHODS if not hasattr(driver, method)]
    if not absent:
        return []
    return [
        f"{type(driver).__name__}: window_control='runtime' объявлен, а методов WindowControl нет: "
        + ", ".join(absent)
    ]


def _default_problems(
    driver: BaseDriver[Any, Any, Any], capabilities: DriverCapabilities, *, attaches: bool
) -> list[str]:
    """Объявлено, а метод остался умолчанием `BaseDriver`."""
    name = type(driver).__name__
    problems: list[str] = []
    if capabilities.state_support != "none" and _inherited(driver, "export_state"):
        problems.append(
            f"{name}: state_support={capabilities.state_support!r} объявлен, а export_state не "
            "переопределён — состояние сессий не сохранялось бы"
        )
    if attaches and _inherited(driver, "attach"):
        problems.append(
            f"{name}: пулу дан провайдер эндпоинтов, а attach не переопределён — подключаться нечем"
        )
    return problems


def uses_default(driver: object, method: str) -> bool:
    """Метод драйвера — умолчание `BaseDriver` («не умею»), а не своя реализация."""
    return isinstance(driver, BaseDriver) and _inherited(
        cast("BaseDriver[Any, Any, Any]", driver), method
    )


def _inherited(driver: BaseDriver[Any, Any, Any], method: str) -> bool:
    return getattr(type(driver), method) is getattr(BaseDriver, method)


__all__ = [
    "CONTEXT_SETTINGS",
    "DEBUG_OPTIONS",
    "BaseDriver",
    "ContextSpec",
    "Driver",
    "DriverCapabilities",
    "Endpoint",
    "EndpointKind",
    "Evidence",
    "FingerprintScope",
    "LaunchSpec",
    "PageLabeler",
    "ProxyScope",
    "StateSupport",
    "WindowBounds",
    "WindowControl",
    "WindowControlLevel",
    "WindowId",
    "WindowState",
    "driver_problems",
    "uses_default",
]
