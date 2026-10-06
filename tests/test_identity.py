"""Identity и её политики: ключ, вариант, прокси, состояние, секреты."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from browser_pool.geometry import Viewport
from browser_pool.identity import ContextOptions, Identity, ProxyPolicy, StatePolicy
from browser_pool.proxies import Proxy
from browser_pool.state import Cookie, Origin, SessionState

if TYPE_CHECKING:
    from collections.abc import Callable

SECRET_PROXY = Proxy(host="1.2.3.4", port=1080, username="user-session-7", password="hunter2")


# --- identity --------------------------------------------------------------------------


def test_identity_defaults() -> None:
    identity = Identity(key="mail:42")

    assert identity.proxy == ProxyPolicy.pool()
    assert identity.state == StatePolicy()
    assert identity.max_pages is None
    assert dict(identity.labels) == {}


def test_payload_and_proxy_credentials_never_rendered() -> None:
    identity = Identity(
        key="mail:42",
        payload={"login": "ada", "password": "s3cret"},
        proxy=ProxyPolicy.fixed(SECRET_PROXY),
    )

    rendered = repr(identity)

    assert "mail:42" in rendered
    assert "s3cret" not in rendered
    assert "hunter2" not in rendered
    assert "user-session-7" not in rendered


def test_labels_are_copied_and_frozen() -> None:
    labels = {"service": "mail"}
    identity = Identity(key="mail:42", labels=labels)
    labels["service"] = "shop"

    assert identity.labels["service"] == "mail"
    with pytest.raises(TypeError):
        identity.labels["service"] = "shop"  # pyright: ignore[reportIndexIssue] — проверяем защиту


def test_identity_equality_is_key_and_variant_only() -> None:
    first = Identity(key="mail:42", variant="proxy", payload={"x": 1})
    same = Identity(key="mail:42", variant="proxy", payload={"x": 2}, max_pages=3)
    assert first == same
    assert hash(first) == hash(same)
    assert len({first, same}) == 1
    assert first != Identity(key="mail:42", variant="direct")
    assert first != Identity(key="mail:43", variant="proxy")
    assert first != "mail:42"


def test_identity_is_hashable_even_with_unhashable_payload() -> None:
    # Идентичность — ключ и вариант; payload может быть чем угодно, хоть словарём.
    first = Identity(key="mail:42", variant="proxy", payload={"x": 1})
    second = Identity(key="mail:42", variant="proxy", payload={"x": 1})

    assert first == second
    assert len({first, second}) == 1
    assert hash(first) != hash(Identity(key="mail:42", variant="direct"))


def test_identity_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        Identity(key="mail:42").key = "mail:43"  # pyright: ignore[reportAttributeAccessIssue] — проверяем защиту


@pytest.mark.parametrize(
    ("build", "fragment"),
    [
        (lambda: Identity(key=""), "key"),
        (lambda: Identity(key="   "), "key"),
        (lambda: Identity(key="mail:1", max_pages=0), "max_pages"),
        # Типизатор ловит это и сам; проверка в рантайме — для нетипизированного вызывающего.
        (lambda: Identity(key="mail:1", variant=["unhashable"]), "variant"),  # pyright: ignore[reportArgumentType]
    ],
)
def test_invalid_identity_rejected(build: Callable[[], object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        build()


def test_context_options_travel_with_identity() -> None:
    options = ContextOptions(locale="de-DE", viewport=Viewport(width=1024, height=768))

    assert Identity(key="shop:1", context_options=options).context_options is options


# --- политики --------------------------------------------------------------------------


def test_proxy_policies() -> None:
    assert ProxyPolicy.pool().mode == "pool"
    assert ProxyPolicy.direct().mode == "direct"
    assert ProxyPolicy.sticky().mode == "sticky"
    assert ProxyPolicy.external().mode == "external"
    fixed = ProxyPolicy.fixed(SECRET_PROXY)
    assert fixed.mode == "fixed"
    assert fixed.proxy is SECRET_PROXY


@pytest.mark.parametrize(
    ("build", "fragment"),
    [
        (lambda: ProxyPolicy(mode="fixed"), "proxy"),
        (lambda: ProxyPolicy(mode="pool", proxy=SECRET_PROXY), "proxy"),
    ],
)
def test_invalid_proxy_policy_rejected(build: Callable[[], object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        build()


def test_state_policy_modes() -> None:
    initial = SessionState(cookies=(Cookie(name="sid", value="v", domain=".example.com"),))

    assert StatePolicy().mode == "read_write"
    assert StatePolicy(mode="read_only", initial=initial).initial is initial
    assert StatePolicy(user_data_dir=Path("profiles/42")).user_data_dir == Path("profiles/42")


# --- состояние сессии ------------------------------------------------------------------


def test_cookie_value_never_rendered() -> None:
    cookie = Cookie(name="sid", value="s3cret", domain=".example.com")

    assert "s3cret" not in repr(cookie)
    assert "sid" in repr(cookie)


def test_session_state_contents_never_rendered() -> None:
    state = SessionState(
        cookies=(Cookie(name="sid", value="s3cret", domain=".example.com"),),
        origins=(Origin(origin="https://example.com", local_storage=(("token", "s3cret"),)),),
        extras={"authorization": "Bearer s3cret"},
    )

    assert "s3cret" not in repr(state)
    assert not state.is_empty()
    assert SessionState().is_empty()


def test_session_state_extras_are_copied_and_frozen() -> None:
    extras = {"authorization": "Bearer a"}
    state = SessionState(extras=extras)
    extras["authorization"] = "Bearer b"

    assert state.extras["authorization"] == "Bearer a"
    with pytest.raises(TypeError):
        state.extras["authorization"] = "Bearer b"  # pyright: ignore[reportIndexIssue] — проверяем защиту


def test_cookie_expiry_is_normalised_to_utc() -> None:
    moscow = timezone(timedelta(hours=3))
    cookie = Cookie(
        name="sid", value="v", domain="example.com", expires=datetime(2030, 1, 1, 3, tzinfo=moscow)
    )

    assert cookie.expires == datetime(2030, 1, 1, tzinfo=UTC)
    assert cookie.expires is not None
    assert cookie.expires.tzinfo is UTC
    assert not cookie.is_session


@pytest.mark.parametrize(
    ("build", "fragment"),
    [
        (lambda: Cookie(name="", value="v", domain="example.com"), "name"),
        (lambda: Cookie(name="sid", value="v", domain=""), "domain"),
        # Наивное время: неизвестно, чей это полдень, — срок куки был бы угадан.
        (
            lambda: Cookie(name="sid", value="v", domain="x.com", expires=datetime(2030, 1, 1)),  # noqa: DTZ001 — проверяем отказ
            "expires",
        ),
        (lambda: Origin(origin="example.com"), "origin"),
    ],
)
def test_invalid_state_values_rejected(build: Callable[[], object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        build()
