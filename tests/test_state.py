"""Состояние сессии: разбор форматов, запись identity, хранилища с оптимистичной блокировкой."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from browser_pool.state import (
    CallbackStateStore,
    Cookie,
    FileStateStore,
    IdentityRecord,
    MemoryStateStore,
    Origin,
    SessionState,
    StaleRecordError,
    StateFormatError,
    StateStore,
    dump_record,
    load_record,
)

EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)

PLAYWRIGHT = json.dumps(
    {
        "cookies": [
            {
                "name": "sid",
                "value": "s3cret",
                "domain": ".example.com",
                "path": "/",
                "expires": EXPIRES.timestamp(),
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {"name": "tmp", "value": "1", "domain": "example.com", "path": "/", "expires": -1},
        ],
        "origins": [
            {
                "origin": "https://example.com",
                "localStorage": [{"name": "token", "value": "abc"}],
            }
        ],
    }
)

EXTENSION = json.dumps(
    [
        {
            "name": "sid",
            "value": "s3cret",
            "domain": ".example.com",
            "path": "/",
            "expirationDate": EXPIRES.timestamp(),
            "hostOnly": False,
            "httpOnly": True,
            "secure": True,
            "sameSite": "no_restriction",
            "session": False,
        },
        {"name": "lang", "value": "ru", "session": True, "sameSite": "unspecified"},
    ]
)

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    f"#HttpOnly_.example.com\tTRUE\t/\tTRUE\t{int(EXPIRES.timestamp())}\tsid\ts3cret\n"
    "example.com\tFALSE\t/\tFALSE\t0\tlang\tru\n"
)


# --- разбор ----------------------------------------------------------------------------


def test_playwright_state_is_taken_whole() -> None:
    state = SessionState.parse(PLAYWRIGHT, default_domain="example.com")

    sid, tmp = state.cookies
    assert sid == Cookie(
        name="sid",
        value="s3cret",
        domain=".example.com",
        expires=EXPIRES,
        http_only=True,
        secure=True,
        same_site="Lax",
    )
    assert tmp.is_session
    assert state.origins == (
        Origin(origin="https://example.com", local_storage=(("token", "abc"),)),
    )


def test_extension_export_is_understood() -> None:
    sid, lang = SessionState.parse(EXTENSION, default_domain="example.com").cookies

    assert sid.expires == EXPIRES
    assert sid.same_site == "None"
    assert sid.http_only
    assert lang.domain == "example.com"  # домена в экспорте нет — берётся по умолчанию
    assert lang.is_session
    assert lang.same_site is None


def test_netscape_file_is_understood_with_http_only_marks() -> None:
    sid, lang = SessionState.parse(NETSCAPE, default_domain="example.com").cookies

    assert sid.domain == ".example.com"
    assert sid.http_only
    assert sid.secure
    assert sid.expires == EXPIRES
    assert lang.is_session
    assert not lang.http_only


def test_header_string_is_understood() -> None:
    state = SessionState.parse("sid=s3cret; lang=ru\nextra = 1", default_domain=".example.com")

    assert [(cookie.name, cookie.value) for cookie in state.cookies] == [
        ("sid", "s3cret"),
        ("lang", "ru"),
        ("extra", "1"),
    ]
    assert all(cookie.domain == ".example.com" for cookie in state.cookies)


def test_byte_order_mark_and_whitespace_are_tolerated() -> None:
    state = SessionState.parse("﻿  " + PLAYWRIGHT + "\n", default_domain="example.com")

    assert len(state.cookies) == 2


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "пуст"),
        ("   ", "пуст"),
        ("{not json", "JSON"),
        ('{"origins": []}', "cookies"),
        ("[1, 2]", "cookie"),
        ("просто текст без знака равенства", "имя=значение"),
    ],
)
def test_unparseable_input_is_explained(raw: str, fragment: str) -> None:
    with pytest.raises(StateFormatError, match=fragment):
        SessionState.parse(raw, default_domain="example.com")


def test_format_error_is_a_value_error() -> None:
    assert issubclass(StateFormatError, ValueError)


# --- запись identity -------------------------------------------------------------------

RECORD = IdentityRecord(
    key="mail:42",
    state=SessionState.parse(PLAYWRIGHT, default_domain="example.com").with_extras(
        {"authorization": "Bearer s3cret"}
    ),
    proxy_ref="db-17",
    fingerprint={"ua": "Mozilla"},
    user_data={"from_name": "Ada"},
    updated_at=EXPIRES,
)


def test_record_round_trips_through_plain_data() -> None:
    data = dump_record(RECORD)

    assert json.loads(json.dumps(data)) == data  # только JSON-типы
    assert load_record(data) == RECORD


def test_record_hides_its_contents() -> None:
    rendered = repr(RECORD)

    assert "s3cret" not in rendered
    assert "Mozilla" not in rendered
    assert "Ada" not in rendered
    assert "mail:42" in rendered


def test_record_moment_must_be_aware() -> None:
    with pytest.raises(ValueError, match="updated_at"):
        IdentityRecord(key="k", updated_at=datetime(2030, 1, 1))  # noqa: DTZ001 — проверяем отказ


# --- хранилища -------------------------------------------------------------------------


@pytest.fixture(params=["memory", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> StateStore:
    if request.param == "memory":
        return MemoryStateStore()
    return FileStateStore(tmp_path / "state")


async def test_store_saves_new_versions_and_loads_them(store: StateStore) -> None:
    assert await store.load("mail:42") is None

    first = await store.save(RECORD)
    second = await store.save(first)

    assert first.version == 1
    assert second.version == 2
    loaded = await store.load("mail:42")
    assert loaded == second


async def test_store_refuses_a_stale_version(store: StateStore) -> None:
    first = await store.save(RECORD)
    await store.save(first)

    with pytest.raises(StaleRecordError) as caught:
        await store.save(first)

    assert caught.value.key == "mail:42"
    assert caught.value.expected == 1
    assert caught.value.actual == 2


async def test_store_deletes(store: StateStore) -> None:
    await store.save(RECORD)

    await store.delete("mail:42")
    await store.delete("mail:42")  # повторно — не ошибка

    assert await store.load("mail:42") is None


async def test_file_store_survives_a_failed_write(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    saved = await store.save(RECORD)
    broken = IdentityRecord(key="mail:42", user_data={"not json": object()}, version=saved.version)

    with pytest.raises(TypeError):
        await store.save(broken)

    assert await store.load("mail:42") == saved
    assert [path.name for path in tmp_path.iterdir()] == [
        path.name for path in tmp_path.glob("*.json")
    ]


async def test_file_store_names_are_safe_for_any_key(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    key = "gmx:ada@example.com/profile:1"

    await store.save(IdentityRecord(key=key))

    (path,) = tmp_path.iterdir()
    assert ":" not in path.name
    assert "/" not in path.name
    loaded = await store.load(key)
    assert loaded is not None
    assert loaded.key == key


async def test_callback_store_passes_records_through() -> None:
    rows: dict[str, dict[str, Any]] = {}

    async def load(key: str) -> dict[str, Any] | None:
        return rows.get(key)

    async def save(key: str, data: dict[str, Any], expected_version: int) -> None:
        current = rows.get(key, {"version": 0})["version"]
        if current != expected_version:
            raise StaleRecordError(key=key, expected=expected_version, actual=current)
        rows[key] = data

    store = CallbackStateStore(load=load, save=save)

    saved = await store.save(RECORD)
    assert saved.version == 1
    assert await store.load("mail:42") == saved
    with pytest.raises(StaleRecordError):
        await store.save(RECORD)
