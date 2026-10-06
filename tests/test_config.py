"""Конфиг пула: дефолты, загрузка из словаря, валидация при создании, пресеты."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta
from typing import Any

import pytest

from browser_pool.config import (
    Backoff,
    Debug,
    GroupLimit,
    Lifecycle,
    Limits,
    PoolConfig,
    Recovery,
    Recycling,
    Timeouts,
    Topology,
    Windows,
)
from browser_pool.errors import ConfigError, PoolError
from browser_pool.geometry import Rect

CUSTOM = PoolConfig(
    topology=Topology(browsers=3, min_browsers=1, pages_per_browser=6, pages_per_identity=3),
    limits=Limits(
        max_waiting=200,
        lease_max_duration=900.0,
        concurrent_opens_per_proxy=None,
        groups=(
            GroupLimit(label="service", value="mail", max_pages=16, max_contexts=20),
            GroupLimit(label="service", value="shop", max_pages=8),
        ),
    ),
    recycling=Recycling(browser_max_leases=None),
    timeouts=Timeouts(open=300.0, acquire=30.0),
    windows=Windows(
        mode="per_context",
        size=(900, 700),
        screen=(
            Rect(x=0, y=0, width=1920, height=1040),
            Rect(x=1920, y=0, width=1280, height=1000),
        ),
        overflow="cascade",
    ),
    debug=Debug(label_windows=True, hold_on_error=60.0, slow_mo=150.0),
)


# --- дефолты ---------------------------------------------------------------------------


def test_pool_works_without_any_config() -> None:
    config = PoolConfig()

    assert config.topology.capacity == 2 * 8
    assert config.limits.max_waiting is None
    # Дефолты — для долгоживущих аккаунтов: счётчики одноразовых сессий выключены.
    assert config.recycling.context_max_leases is None
    assert config.recycling.context_max_error_score is None


def test_config_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        PoolConfig().topology.browsers = 5  # pyright: ignore[reportAttributeAccessIssue] — проверяем защиту


# --- словарь туда и обратно ------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [PoolConfig(), CUSTOM, PoolConfig.accounts(), PoolConfig.scraping()],
    ids=["default", "custom", "accounts", "scraping"],
)
def test_mapping_round_trip(config: PoolConfig) -> None:
    assert PoolConfig.from_mapping(config.to_mapping()) == config


def test_mapping_is_plain_data() -> None:
    mapping = CUSTOM.to_mapping()

    assert mapping["topology"]["browsers"] == 3
    assert mapping["limits"]["groups"][1] == {
        "label": "service",
        "value": "shop",
        "max_pages": 8,
        "max_contexts": None,
    }


def test_missing_keys_take_defaults() -> None:
    config = PoolConfig.from_mapping({"topology": {"browsers": 4}})

    assert config.topology.browsers == 4
    assert config.topology.pages_per_browser == Topology().pages_per_browser
    assert config.limits == Limits()


def test_seconds_accept_timedelta_and_integers() -> None:
    config = PoolConfig.from_mapping(
        {"lifecycle": {"context_idle_ttl": timedelta(minutes=15), "page_idle_ttl": 60}}
    )

    assert config.lifecycle.context_idle_ttl == 900.0
    assert isinstance(config.lifecycle.page_idle_ttl, float)


def test_unknown_keys_are_errors_with_hints() -> None:
    with pytest.raises(ConfigError) as caught:
        PoolConfig.from_mapping(
            {"topology": {"browser": 3}, "limits": {"max_wating": 5}, "resorces": {}}
        )

    message = str(caught.value)
    # Все проблемы разом, с путём и ближайшим правильным именем.
    assert "topology.browser" in message
    assert "browsers" in message
    assert "limits.max_wating" in message
    assert "max_waiting" in message
    assert "resorces" in message
    assert len(caught.value.problems) == 3


@pytest.mark.parametrize(
    ("mapping", "path"),
    [
        ({"topology": {"browsers": "3"}}, "topology.browsers"),
        ({"topology": {"browsers": True}}, "topology.browsers"),  # bool — не число браузеров
        ({"topology": {"browsers": 2.5}}, "topology.browsers"),
        ({"lifecycle": {"browser_idle_ttl": "5m"}}, "lifecycle.browser_idle_ttl"),
        ({"topology": 3}, "topology"),
        ({"limits": {"groups": {"label": "x"}}}, "limits.groups"),
        ({"limits": {"groups": [{"label": "service"}]}}, "limits.groups[0].value"),
    ],
)
def test_wrong_types_are_reported_with_path(mapping: dict[str, Any], path: str) -> None:
    with pytest.raises(ConfigError, match=path.replace("[", r"\[").replace("]", r"\]")):
        PoolConfig.from_mapping(mapping)


def test_config_error_is_a_pool_error_and_a_value_error() -> None:
    assert issubclass(ConfigError, PoolError)
    assert issubclass(ConfigError, ValueError)


# --- валидация при создании ------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "fragment"),
    [
        (lambda: Topology(browsers=0), "browsers"),
        (lambda: Topology(browsers=2, min_browsers=3), "min_browsers"),
        (lambda: Topology(pages_per_browser=0), "pages_per_browser"),
        (lambda: Topology(contexts_per_browser=0), "contexts_per_browser"),
        (lambda: Topology(pages_per_identity=0), "pages_per_identity"),
        (
            lambda: Topology(pages_per_browser=4, warm_pages_per_identity=5),
            "warm_pages_per_identity",
        ),
        (
            lambda: Topology(pages_per_identity=2, warm_pages_per_identity=3),
            "warm_pages_per_identity",
        ),
        (lambda: Lifecycle(browser_idle_ttl=0.0), "browser_idle_ttl"),
        (lambda: Limits(max_waiting=-1), "max_waiting"),
        (lambda: Timeouts(acquire=0.0), "acquire"),
        (lambda: Limits(concurrent_launches=0), "concurrent_launches"),
        (lambda: Limits(spawn_delay=-0.1), "spawn_delay"),
        (lambda: Limits(concurrent_opens=0), "concurrent_opens"),
        (lambda: Limits(concurrent_opens_per_proxy=0), "concurrent_opens_per_proxy"),
        (lambda: Limits(max_identities_per_proxy=0), "max_identities_per_proxy"),
        (
            lambda: Limits(
                groups=(GroupLimit(label="s", value="a", max_pages=1),) * 2,
            ),
            "groups",
        ),
        (lambda: GroupLimit(label="s", value="a"), "max_pages"),
        (lambda: GroupLimit(label="s", value="a", max_pages=0), "max_pages"),
        (lambda: GroupLimit(label="", value="a", max_pages=1), "label"),
        (lambda: Recycling(browser_max_leases=0), "browser_max_leases"),
        (lambda: Recycling(recycle_jitter=0.5), "recycle_jitter"),
        (lambda: Lifecycle(healthcheck_interval=0.0), "healthcheck_interval"),
        (lambda: Recovery(restart_backoff=Backoff(initial=10.0, maximum=5.0)), "maximum"),
        (lambda: Recovery(proxy_retries=-1), "proxy_retries"),
        (lambda: Limits(leak_warn_after=600.0, lease_max_duration=300.0), "leak_warn_after"),
        (lambda: Recycling(context_max_leases=0), "context_max_leases"),
        (lambda: Recycling(context_error_score_decrement=-1.0), "context_error_score_decrement"),
        (lambda: Timeouts(open=0.0), "open"),
        (lambda: Timeouts(kill=float("nan")), "kill"),
        (lambda: Timeouts(close=float("inf")), "close"),
    ],
)
def test_invalid_values_fail_at_creation(build: Any, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        build()


def test_invalid_values_fail_from_mapping_too() -> None:
    with pytest.raises(ConfigError, match="min_browsers"):
        PoolConfig.from_mapping({"topology": {"browsers": 1, "min_browsers": 2}})


def test_fewer_contexts_than_pages_is_allowed() -> None:
    # Контекст держит несколько вкладок: K < T — законная конфигурация.
    assert Topology(pages_per_browser=8, contexts_per_browser=2).contexts_per_browser == 2


# --- производные конфиги ---------------------------------------------------------------


def test_replace_swaps_sections_and_keeps_the_rest() -> None:
    debug = CUSTOM.replace(topology=Topology(browsers=1))

    assert debug.topology.browsers == 1
    assert debug.limits is CUSTOM.limits
    assert CUSTOM.topology.browsers == 3


def test_presets() -> None:
    accounts = PoolConfig.accounts()
    scraping = PoolConfig.scraping()

    assert accounts.topology.warm_pages_per_identity >= 1
    assert accounts.limits.concurrent_opens_per_proxy == 1
    assert accounts.recycling.context_max_leases is None

    assert scraping.topology.warm_pages_per_identity == 0
    assert scraping.lifecycle.context_idle_ttl is not None
    assert accounts.lifecycle.context_idle_ttl is not None
    assert scraping.lifecycle.context_idle_ttl < accounts.lifecycle.context_idle_ttl
    assert scraping.recycling.context_max_leases is not None
    assert scraping.recycling.context_max_error_score is not None


# --- окна и отладка --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        ("auto", "auto"),
        (1, 1),
        ({"x": 0, "y": 0, "width": 800, "height": 600}, Rect(x=0, y=0, width=800, height=600)),
        ([{"x": 0, "y": 0, "width": 800, "height": 600}], (Rect(x=0, y=0, width=800, height=600),)),
    ],
)
def test_screen_takes_any_of_its_forms(screen: object, expected: object) -> None:
    config = PoolConfig.from_mapping({"windows": {"screen": screen}})

    assert config.windows.screen == expected


def test_window_settings_are_checked() -> None:
    with pytest.raises(ConfigError) as caught:
        PoolConfig.from_mapping(
            {
                "windows": {"size": [1], "snap": "yes", "screen": "left"},
                "debug": {"hold_on_error": -1},
            }
        )

    problems = " | ".join(caught.value.problems)
    assert "windows.size" in problems
    assert "windows.snap" in problems
    assert "windows.screen" in problems
    assert "hold_on_error" in problems


def test_window_size_below_minimum_is_refused() -> None:
    with pytest.raises(ConfigError, match="min_size"):
        Windows(size=(300, 200))


def test_windows_are_off_by_default() -> None:
    assert not PoolConfig().windows.active
    assert PoolConfig().replace(windows=Windows(mode="per_page")).windows.active
