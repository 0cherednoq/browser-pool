"""Хранилища и разбор сессии (M8.10): файлы без коллизий, `str` вместо `Path`, только `StateFormatError`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from browser_pool import FileIdentityLock, StatePolicy
from browser_pool.locks import FileLock
from browser_pool.state import (
    CallbackStateStore,
    Cookie,
    FileStateStore,
    IdentityRecord,
    SessionState,
    StateFormatError,
    StateStore,
)
from browser_pool.state.store import DeleteCallback, LoadCallback, SaveCallback

SID = Cookie(name="sid", value="v", domain="example.com")


def record(key: str, *, version: int = 0, proxy_ref: str | None = None) -> IdentityRecord:
    return IdentityRecord(
        key=key, state=SessionState(cookies=(SID,)), version=version, proxy_ref=proxy_ref
    )


# --- имена файлов ----------------------------------------------------------------------


async def test_keys_differing_only_by_case_are_different_records(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)

    await store.save(record("Ada@x.com", proxy_ref="p1"))

    assert await store.load("ada@x.com") is None  # на Windows и macOS файлы не различают регистр
    saved = await store.save(record("ada@x.com", proxy_ref="p2"))
    assert saved.version == 1
    upper, lower = await store.load("Ada@x.com"), await store.load("ada@x.com")
    assert upper is not None
    assert lower is not None
    assert (upper.proxy_ref, lower.proxy_ref) == ("p1", "p2")


async def test_a_long_non_ascii_key_fits_the_file_system(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    key = "почта:" + "ё" * 120

    await store.save(record(key))

    loaded = await store.load(key)
    assert loaded is not None
    assert loaded.key == key
    assert all(len(path.name) < 200 for path in tmp_path.iterdir())


async def test_a_foreign_record_under_the_name_is_not_taken_for_the_key(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    await store.save(record("mail:a"))
    (path,) = tmp_path.glob("*.json")
    other = json.loads(path.read_text(encoding="utf-8"))
    other["key"] = "mail:someone-else"
    path.write_text(json.dumps(other), encoding="utf-8")

    assert await store.load("mail:a") is None


async def test_delete_removes_exactly_the_keys_file(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    await store.save(record("Ada"))
    await store.save(record("ada"))

    await store.delete("Ada")

    assert await store.load("Ada") is None
    assert await store.load("ada") is not None


# --- одновременное чтение и запись -----------------------------------------------------


async def test_load_and_save_at_once_never_fail(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    current = await store.save(record("mail:a"))
    failures: list[Exception] = []

    async def writer() -> None:
        nonlocal current
        for _ in range(60):
            try:
                current = await store.save(
                    IdentityRecord(key="mail:a", state=current.state, version=current.version)
                )
            except Exception as error:  # noqa: BLE001 — ловим любой сбой, чтобы показать его в проверке
                failures.append(error)

    async def reader() -> None:
        for _ in range(120):
            try:
                assert await store.load("mail:a") is not None
            except Exception as error:  # noqa: BLE001 — ловим любой сбой, чтобы показать его в проверке
                failures.append(error)

    await asyncio.gather(writer(), *(reader() for _ in range(4)))

    assert failures == []
    assert current.version == 61


# --- CallbackStateStore ----------------------------------------------------------------


async def test_callback_store_accepts_plain_functions() -> None:
    rows: dict[str, tuple[dict[str, Any], int]] = {}

    def load(key: str) -> dict[str, Any] | None:
        return rows[key][0] if key in rows else None

    def save(key: str, data: dict[str, Any], expected: int) -> None:
        rows[key] = (data, expected)

    def delete(key: str) -> None:
        rows.pop(key, None)

    store: StateStore = CallbackStateStore(load=load, save=save, delete=delete)

    saved = await store.save(record("mail:a"))
    assert (await store.load("mail:a")) == saved
    await store.delete("mail:a")
    assert await store.load("mail:a") is None


def test_callback_aliases_can_be_introspected() -> None:
    for alias in (LoadCallback, SaveCallback, DeleteCallback):
        assert alias.__value__ is not None


# --- str вместо Path -------------------------------------------------------------------


async def test_file_locks_accept_a_string_path(tmp_path: Path) -> None:
    identity_lock = FileIdentityLock(str(tmp_path / "locks"))
    await identity_lock.acquire("mail:a")
    assert identity_lock.held("mail:a")
    await identity_lock.release("mail:a")

    file_lock = FileLock(str(tmp_path / "one.lock"))
    await file_lock.acquire()
    assert file_lock.held
    file_lock.release()


def test_state_policy_turns_string_paths_into_paths() -> None:
    policy = StatePolicy(user_data_dir="profiles/a", profile_template="profiles/template")  # pyright: ignore[reportArgumentType]

    assert policy.user_data_dir == Path("profiles/a")
    assert policy.profile_template == Path("profiles/template")


# --- разбор сессии ---------------------------------------------------------------------

BROKEN_INPUTS = [
    '[{"name": "a", "value": "b", "domain": "x.com", "expires": 1e300}]',
    '{"cookies": [{"name": "a", "value": "b", "domain": "x.com", "expires": 1e300}]}',
    '{"cookies": [], "origins": [{"origin": "https://x", "localStorage": ["не пара"]}]}',
    '{"cookies": [], "origins": [{"origin": "https://x", "localStorage": 5}]}',
    '{"cookies": [{"name": "a", "value": "b"}]}',
    '[{"name": "a", "value": "b"}]',
    '[{"name": "a", "value": "b", "domain": "x.com", "expires": "завтра"}]',
    "[1, 2, 3]",
    '{"cookies": "нет"}',
    "{",
    "x.com\tTRUE\t/\tFALSE\t99999999999999999999\tname\tvalue",
    "\t\t\t\t\t\t",
    "# Netscape HTTP Cookie File\nбитая\tстрока",
    "\x00\x01\x02",
]


@pytest.mark.parametrize("raw", BROKEN_INPUTS)
def test_unparseable_sessions_raise_only_state_format_error(raw: str) -> None:
    try:
        SessionState.parse(raw)
    except StateFormatError:
        return
    # Разобралось (например, мусорная строка стала строкой кук) — тоже не исключение чужого типа.


def test_an_empty_netscape_cookie_value_is_kept() -> None:
    text = "# Netscape HTTP Cookie File\nexample.com\tTRUE\t/\tFALSE\t0\tflag\t\nexample.com\tTRUE\t/\tFALSE\t0\tsid\tabc\n"

    state = SessionState.parse(text)

    assert [(c.name, c.value) for c in state.cookies] == [("flag", ""), ("sid", "abc")]


def test_default_domain_is_optional_when_the_format_carries_domains() -> None:
    playwright = json.dumps({"cookies": [{"name": "a", "value": "b", "domain": "x.com"}]})
    listed = json.dumps([{"name": "a", "value": "b", "domain": "x.com"}])

    assert SessionState.parse(playwright).cookies[0].domain == "x.com"
    assert SessionState.parse(listed).cookies[0].domain == "x.com"


def test_header_cookies_without_a_domain_explain_what_is_missing() -> None:
    with pytest.raises(StateFormatError, match="default_domain"):
        SessionState.parse("sid=abc; theme=dark")

    state = SessionState.parse("sid=abc", default_domain=".example.com")
    assert state.cookies[0].domain == ".example.com"
