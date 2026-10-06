"""Публичные значения проверяют `Literal`-поля в рантайме, а не полагаются на типизатор (M7.09)."""

from __future__ import annotations

from typing import Any

import pytest

from browser_pool import ProxyPolicy, StatePolicy
from browser_pool.config import Resources, Windows
from browser_pool.driver import DriverCapabilities, Endpoint
from browser_pool.drivers.pydoll import PydollDriver
from browser_pool.errors import ConfigError
from browser_pool.proxies import ProxyOutcome
from browser_pool.state import Cookie

type Case = tuple[str, type[Any], dict[str, Any], type[ValueError], str]

CASES: list[Case] = [
    ("ProxyPolicy.mode", ProxyPolicy, {"mode": "bogus"}, ValueError, "'pool'"),
    ("StatePolicy.mode", StatePolicy, {"mode": "bogus"}, ValueError, "'read_write'"),
    ("Windows.mode", Windows, {"mode": "bogus"}, ConfigError, "'per_context'"),
    ("Windows.layout", Windows, {"layout": "bogus"}, ConfigError, "'grid'"),
    ("Windows.overflow", Windows, {"overflow": "bogus"}, ConfigError, "'tabs'"),
    ("Windows.reflow", Windows, {"reflow": "bogus"}, ConfigError, "'fill'"),
    ("Windows.order", Windows, {"order": "bogus"}, ConfigError, "'created'"),
    ("Windows.group", Windows, {"group": "bogus"}, ConfigError, "'none'"),
    ("Resources.pressure_action", Resources, {"pressure_action": "bogus"}, ConfigError, "'hold'"),
    (
        "DriverCapabilities.proxy_scope",
        DriverCapabilities,
        {"proxy_scope": "bogus"},
        ConfigError,
        "'context'",
    ),
    (
        "DriverCapabilities.state_support",
        DriverCapabilities,
        {"proxy_scope": "external", "state_support": "bogus"},
        ConfigError,
        "'cookies'",
    ),
    (
        "DriverCapabilities.window_control",
        DriverCapabilities,
        {"proxy_scope": "external", "window_control": "bogus"},
        ConfigError,
        "'launch_only'",
    ),
    (
        "DriverCapabilities.fingerprint_scope",
        DriverCapabilities,
        {"proxy_scope": "external", "fingerprint_scope": "bogus"},
        ConfigError,
        "'browser'",
    ),
    ("Endpoint.kind", Endpoint, {"kind": "bogus", "url": "ws://x"}, ValueError, "'cdp'"),
    ("ProxyOutcome.kind", ProxyOutcome, {"kind": "bogus"}, ValueError, "'banned'"),
    (
        "Cookie.same_site",
        Cookie,
        {"name": "a", "value": "b", "domain": "example.com", "same_site": "bogus"},
        ValueError,
        "'Lax'",
    ),
    ("PydollDriver.channel", PydollDriver, {"channel": "bogus"}, ValueError, "'edge'"),
]


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_unknown_literal_value_is_rejected_with_the_allowed_ones(case: Case) -> None:
    field, factory, kwargs, error, hint = case
    with pytest.raises(error, match="bogus") as caught:
        factory(**kwargs)
    assert field in str(caught.value)
    assert hint in str(caught.value)
