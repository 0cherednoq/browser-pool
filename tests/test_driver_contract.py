"""Контракт драйвера: протокол, возможности, спецификации запуска и контекста."""

from __future__ import annotations

import dataclasses
import importlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest

from browser_pool import BrowserPool, ContextOptions, Identity, PoolConfig, StatePolicy
from browser_pool.config import Debug
from browser_pool.driver import (
    BaseDriver,
    ContextSpec,
    Driver,
    DriverCapabilities,
    Endpoint,
    Evidence,
    LaunchSpec,
    WindowBounds,
    WindowControl,
    WindowId,
    WindowState,
    driver_problems,
)
from browser_pool.errors import ConfigError, ErrorKind, UnsupportedRequirementError
from browser_pool.geometry import Geolocation, Rect, Viewport
from browser_pool.proxies import Proxy
from browser_pool.state import Cookie, SessionState
from browser_pool.testing import FAKE_CAPABILITIES, FakeDriver, FakeEndpointProvider

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# --- минимальная реализация: протокол проверяется типизатором на ней ------------------


class Browser:
    """Браузер пустышки."""


class Context:
    """Контекст пустышки."""


class Page:
    """Вкладка пустышки."""


class MinimalDriver:
    """Всё, что протокол требует, и ничего сверх — если типизатор это примет, контракт выполним."""

    capabilities = DriverCapabilities(proxy_scope="context", can_new_context=True)

    async def prepare(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def launch(self, spec: LaunchSpec) -> Browser:
        return Browser()

    async def attach(self, endpoint: Endpoint) -> Browser:
        return Browser()

    async def ping(self, browser: Browser) -> bool:
        return True

    def on_disconnect(self, browser: Browser, callback: Callable[[], None]) -> None:
        return None

    async def close_browser(self, browser: Browser) -> None:
        return None

    async def kill_browser(self, browser: Browser) -> None:
        return None

    def pid(self, browser: Browser) -> int | None:
        return None

    async def new_context(self, browser: Browser, spec: ContextSpec) -> Context:
        return Context()

    async def close_context(self, context: Context) -> None:
        return None

    async def export_state(self, context: Context) -> SessionState:
        return SessionState()

    async def add_cookies(self, context: Context, cookies: Sequence[Cookie]) -> None:
        return None

    async def new_page(self, context: Context) -> Page:
        return Page()

    def page_usable(self, page: Page) -> bool:
        return True

    async def close_page(self, page: Page) -> None:
        return None

    async def capture(self, page: Page) -> Evidence:
        return Evidence()

    def classify(self, error: BaseException) -> ErrorKind | None:
        return None


class WindowedDriver(MinimalDriver):
    """Драйвер, который вдобавок двигает окна."""

    capabilities = DriverCapabilities(
        proxy_scope="context", can_new_context=True, new_window=True, window_control="runtime"
    )

    async def window_of(self, page: Page) -> WindowId:
        return 1

    async def get_bounds(self, browser: Browser, window: WindowId) -> WindowBounds:
        return WindowBounds(rect=Rect(x=0, y=0, width=800, height=600))

    async def set_bounds(self, browser: Browser, window: WindowId, bounds: WindowBounds) -> None:
        return None

    async def screen_area(self, page: Page) -> Rect:
        return Rect(x=0, y=0, width=1920, height=1040)

    async def bring_to_front(self, page: Page) -> None:
        return None


def test_minimal_implementation_satisfies_the_protocol() -> None:
    driver: Driver[Browser, Context, Page] = MinimalDriver()  # статическая проверка — главная

    assert isinstance(driver, Driver)
    assert not isinstance(driver, WindowControl)


def test_window_control_is_an_optional_extra() -> None:
    driver: WindowControl[Browser, Page] = WindowedDriver()

    assert isinstance(driver, WindowControl)
    assert isinstance(driver, Driver)


def test_incomplete_driver_is_not_a_driver() -> None:
    class Incomplete:
        capabilities = DriverCapabilities(proxy_scope="browser")

        async def launch(self, spec: LaunchSpec) -> Browser:
            return Browser()

    assert not isinstance(Incomplete(), Driver)


# --- возможности -----------------------------------------------------------------------


def test_undeclared_capabilities_are_absent() -> None:
    # Драйвер, который о себе ничего не сказал, возможностей авансом не получает.
    capabilities = DriverCapabilities(proxy_scope="browser")

    assert not capabilities.can_new_context
    assert capabilities.state_support == "none"
    assert not capabilities.proxy_auth
    assert capabilities.window_control == "none"
    assert capabilities.max_pages_hint is None


@pytest.mark.parametrize(
    ("build", "fragment"),
    [
        # Прокси на контекст без возможности создать контекст — противоречие.
        (lambda: DriverCapabilities(proxy_scope="context"), "can_new_context"),
        (
            lambda: DriverCapabilities(proxy_scope="browser", proxy_schemes=frozenset({"ftp"})),
            "ftp",
        ),
        (lambda: DriverCapabilities(proxy_scope="browser", new_window=True), "new_window"),
        # Схемы авторизации без самой авторизации и вне схем прокси — противоречие.
        (
            lambda: DriverCapabilities(
                proxy_scope="browser", proxy_auth_schemes=frozenset({"http"})
            ),
            "proxy_auth_schemes",
        ),
        (
            lambda: DriverCapabilities(
                proxy_scope="browser", proxy_auth=True, proxy_auth_schemes=frozenset({"socks5"})
            ),
            "socks5",
        ),
        (lambda: DriverCapabilities(proxy_scope="browser", max_pages_hint=0), "max_pages_hint"),
    ],
)
def test_contradictory_capabilities_are_rejected(
    build: Callable[[], object], fragment: str
) -> None:
    with pytest.raises(ConfigError, match=fragment):
        build()


# --- спецификации: хуки их меняют, а вызовы не делят ------------------------------------


def test_specs_are_mutable_for_hooks() -> None:
    spec = ContextSpec()
    spec.locale = "ru-RU"
    spec.extra["color_scheme"] = "dark"

    assert spec.locale == "ru-RU"
    assert spec.extra == {"color_scheme": "dark"}


def test_specs_do_not_share_extra() -> None:
    first, second = LaunchSpec(), LaunchSpec()
    first.extra["slow_mo"] = 100
    first.args.append("--disable-gpu")

    assert second.extra == {}
    assert second.args == []


def test_context_spec_carries_everything_the_driver_needs() -> None:
    proxy = Proxy(host="1.2.3.4", port=8080)
    spec = ContextSpec(
        proxy=proxy,
        state=SessionState(),
        locale="ru-RU",
        timezone="Europe/Moscow",
        geolocation=Geolocation(latitude=55.75, longitude=37.62),
        viewport=Viewport(width=1280, height=720),
        user_agent="UA",
        default_timeout=15.0,
    )

    assert spec.proxy is proxy


# --- значения --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "fragment"),
    [
        (lambda: Rect(x=0, y=0, width=0, height=10), "width"),
        (lambda: Rect(x=0, y=0, width=10, height=-1), "height"),
        (lambda: Viewport(width=0, height=10), "width"),
        (lambda: Geolocation(latitude=91.0, longitude=0.0), "latitude"),
        (lambda: Geolocation(latitude=0.0, longitude=181.0), "longitude"),
        (lambda: Geolocation(latitude=0.0, longitude=0.0, accuracy=-1.0), "accuracy"),
        (lambda: Endpoint(kind="cdp", url=""), "url"),
    ],
)
def test_invalid_values_rejected(build: Callable[[], object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        build()


def test_window_bounds_default_to_normal_state() -> None:
    bounds = WindowBounds(rect=Rect(x=10, y=20, width=300, height=200))

    assert bounds.state is WindowState.normal
    with pytest.raises(FrozenInstanceError):
        bounds.state = WindowState.minimized  # pyright: ignore[reportAttributeAccessIssue] — проверяем защиту


def test_endpoint_url_is_not_rendered() -> None:
    # URL подключения несёт токен (browserless `?token=`, антидетект-API): в логи не выводить.
    endpoint = Endpoint(kind="cdp", url="ws://host:3000/devtools?token=s3cret", pid=42)

    assert "s3cret" not in repr(endpoint)
    assert "cdp" in repr(endpoint)


def test_evidence_payload_is_not_rendered() -> None:
    evidence = Evidence(
        screenshot=b"\x89PNG" * 1000, html="<input value='s3cret'>", url="https://x"
    )

    rendered = repr(evidence)

    assert "s3cret" not in rendered
    assert len(rendered) < 200


# --- драйверы поставки: режим без окна задаётся одинаково ----------------------------------


@pytest.mark.parametrize("module", ["playwright", "pydoll"])
def test_shipped_drivers_take_headless_the_same_way(module: str) -> None:
    pytest.importorskip("playwright.async_api" if module == "playwright" else "pydoll")
    drivers = importlib.import_module(f"browser_pool.drivers.{module}")
    driver_class = drivers.PlaywrightDriver if module == "playwright" else drivers.PydollDriver

    assert driver_class().headless is None  # решает пул
    assert driver_class(headless=True).headless is True
    assert driver_class(headless=False).headless is False


# --- BaseDriver и проверка драйвера при создании пула ----------------------------------------


class Skeleton(BaseDriver[Browser, Context, Page]):
    """Только обязательное: всё остальное — умолчания `BaseDriver`."""

    declared = DriverCapabilities(proxy_scope="browser")

    @property
    @override
    def capabilities(self) -> DriverCapabilities:
        return self.declared

    @override
    async def launch(self, spec: LaunchSpec) -> Browser:
        return Browser()

    @override
    async def ping(self, browser: Browser) -> bool:
        return True

    @override
    async def close_browser(self, browser: Browser) -> None:
        return None

    @override
    async def new_context(self, browser: Browser, spec: ContextSpec) -> Context:
        return Context()

    @override
    async def close_context(self, context: Context) -> None:
        return None

    @override
    async def new_page(self, context: Context) -> Page:
        return Page()

    @override
    def page_usable(self, page: Page) -> bool:
        return True

    @override
    async def close_page(self, page: Page) -> None:
        return None


async def test_base_driver_needs_only_the_essentials() -> None:
    driver = Skeleton()

    assert isinstance(driver, Driver)
    assert driver_problems(driver) == ()
    assert driver.pid(Browser()) is None
    assert driver.classify(RuntimeError()) is None
    assert (await driver.export_state(Context())).is_empty()
    assert await driver.capture(Page()) == Evidence()
    with pytest.raises(UnsupportedRequirementError, match="cdp"):
        await driver.attach(Endpoint(kind="cdp", url="ws://127.0.0.1:1/devtools"))


def test_base_driver_without_an_essential_cannot_be_created() -> None:
    class NoPages(BaseDriver[Browser, Context, Page]):
        @property
        @override
        def capabilities(self) -> DriverCapabilities:
            return DriverCapabilities(proxy_scope="browser")

    with pytest.raises(TypeError, match="new_page"):
        NoPages()  # pyright: ignore[reportAbstractUsage] — в этом и проверка


def test_declared_state_without_export_state_is_refused_by_the_pool() -> None:
    class Forgetful(Skeleton):
        declared = DriverCapabilities(proxy_scope="browser", state_support="cookies")

    with pytest.raises(ConfigError, match="export_state"):
        BrowserPool(Forgetful())


def test_provider_with_a_driver_that_cannot_attach_is_refused() -> None:
    with pytest.raises(ConfigError, match="attach"):
        BrowserPool(Skeleton(), provider=FakeEndpointProvider())


def test_declared_window_control_without_its_methods_is_refused() -> None:
    class Windowless(Skeleton):
        declared = DriverCapabilities(proxy_scope="browser", window_control="runtime")

    with pytest.raises(ConfigError, match="set_bounds"):
        BrowserPool(Windowless())


def test_object_that_is_not_a_driver_is_refused_with_the_missing_methods() -> None:
    class Incomplete:
        capabilities = DriverCapabilities(proxy_scope="browser")

        async def launch(self, spec: LaunchSpec) -> Browser:
            return Browser()

    with pytest.raises(ConfigError) as refused:
        BrowserPool(Incomplete())  # pyright: ignore[reportArgumentType] — в этом и проверка

    assert "new_page" in str(refused.value)
    assert "close_browser" in str(refused.value)


def test_unknown_setting_names_in_capabilities_are_rejected() -> None:
    with pytest.raises(ConfigError, match="timezone_id"):
        DriverCapabilities(proxy_scope="browser", context_settings=frozenset({"timezone_id"}))
    with pytest.raises(ConfigError, match="turbo"):
        DriverCapabilities(proxy_scope="browser", debug_options=frozenset({"turbo"}))


async def test_identity_asking_for_what_the_driver_does_not_apply_is_refused() -> None:
    plain = dataclasses.replace(FAKE_CAPABILITIES, context_settings=frozenset({"locale"}))
    german = Identity(key="mail:de", context_options=ContextOptions(locale="de-DE"))
    tokyo = Identity(
        key="mail:jp", context_options=ContextOptions(locale="ja-JP", timezone="Asia/Tokyo")
    )

    async with BrowserPool(FakeDriver(capabilities=plain)) as pool:
        async with pool.page(german) as lease:
            assert lease.context.spec.locale == "de-DE"
        with pytest.raises(UnsupportedRequirementError, match="timezone") as refused:
            async with pool.page(tokyo):
                pass

    assert "locale" not in str(refused.value)  # названо только то, чего драйвер не применяет


async def test_context_settings_do_not_reach_a_profile_context(tmp_path: Path) -> None:
    profiled = Identity(
        key="mail:profile",
        state=StatePolicy(mode="none", user_data_dir=tmp_path / "profile"),
        context_options=ContextOptions(timezone="Asia/Tokyo"),
    )

    async with BrowserPool(FakeDriver()) as pool:
        with pytest.raises(UnsupportedRequirementError, match="готовому контексту"):
            async with pool.page(profiled):
                pass


async def test_debug_option_the_driver_cannot_apply_is_called_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plain = dataclasses.replace(FAKE_CAPABILITIES, debug_options=frozenset())
    config = PoolConfig(debug=Debug(slow_mo=50.0))

    async with BrowserPool(FakeDriver(capabilities=plain), config=config):
        pass
    async with BrowserPool(FakeDriver(), config=config):
        pass

    warnings = [record for record in caplog.records if "slow_mo" in record.getMessage()]
    assert len(warnings) == 1
