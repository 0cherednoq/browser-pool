"""`PoolConfig.replace` (M8.20): правка одного поля секции не теряет остальные поля пресета."""

from __future__ import annotations

import pytest

import browser_pool
from browser_pool import Debug, PoolConfig, Resources, Windows
from browser_pool.config import Limits
from browser_pool.errors import ConfigError


def test_a_mapping_changes_only_the_named_fields_of_the_section() -> None:
    preset = PoolConfig.scraping()

    changed = preset.replace(limits={"max_waiting": 50})

    assert changed.limits.max_waiting == 50
    assert changed.limits.concurrent_opens == preset.limits.concurrent_opens == 16
    assert changed.limits.concurrent_opens_per_proxy is None  # поле пресета не потеряно
    assert changed.topology == preset.topology


def test_a_whole_section_still_replaces_it() -> None:
    preset = PoolConfig.scraping()

    changed = preset.replace(limits=Limits(max_waiting=5))

    assert changed.limits.concurrent_opens == Limits().concurrent_opens  # как и раньше — целиком


def test_mapping_works_for_every_section() -> None:
    config = PoolConfig.accounts().replace(
        topology={"browsers": 4},
        lifecycle={"context_idle_ttl": 60.0},
        recycling={"browser_max_age": None},
        recovery={"proxy_retries": 0},
        timeouts={"open": 30.0},
        windows={"mode": "per_context"},
        debug={"slow_mo": 100.0},
        resources={"min_free_memory_mb": 512.0},
    )

    assert config.topology.browsers == 4
    assert config.lifecycle.context_idle_ttl == 60.0
    assert config.recycling.browser_max_age is None
    assert config.recovery.proxy_retries == 0
    assert config.timeouts.open == 30.0
    assert config.windows.mode == "per_context"
    assert config.debug.slow_mo == 100.0
    assert config.resources.min_free_memory_mb == 512.0
    assert config.lifecycle.page_idle_ttl == PoolConfig.accounts().lifecycle.page_idle_ttl


def test_unknown_field_in_a_mapping_is_a_config_error_with_a_hint() -> None:
    with pytest.raises(ConfigError, match=r"limits\.max_waitin.*max_waiting"):
        PoolConfig().replace(limits={"max_waitin": 5})


def test_invalid_value_in_a_mapping_is_validated() -> None:
    with pytest.raises(ConfigError):
        PoolConfig().replace(topology={"browsers": 0})


def test_all_config_sections_are_in_the_root() -> None:
    assert browser_pool.Windows is Windows
    assert browser_pool.Debug is Debug
    assert browser_pool.Resources is Resources


def test_providers_are_importable_from_the_package() -> None:
    from browser_pool.providers import AdsPowerProvider, RemoteCDP
    from browser_pool.providers.adspower import AdsPowerProvider as FromModule

    assert AdsPowerProvider is FromModule
    assert RemoteCDP is not None
