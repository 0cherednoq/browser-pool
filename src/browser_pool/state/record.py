"""Запись identity: всё, что делает её собой, и переживает перезапуск процесса.

Identity владеет своей идентичностью целиком: состояние сессии, закреплённый прокси
(`ProxyPolicy.sticky`), отпечаток, данные site SDK. Запись версионирована: хранилище сохраняет
её, только если сохранённая версия та же, что была при чтении, — иначе `StaleRecordError`, а не
тихая перезапись чужой записи другим процессом.

`dump_record` / `load_record` — запись как простые данные JSON: для файла, БД, очереди.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from browser_pool.clock import utc_now
from browser_pool.state.session_state import Cookie, Origin, SameSite, SessionState

if TYPE_CHECKING:
    from collections.abc import Mapping


class StaleRecordError(Exception):
    """Запись сохраняют поверх более новой: её успел изменить кто-то другой."""

    def __init__(self, *, key: str, expected: int, actual: int) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        super().__init__(f"Запись {key}: сохраняют версию {expected}, а в хранилище уже {actual}")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityRecord:
    """Сохраняемая идентичность identity."""

    key: str
    state: SessionState = field(default_factory=SessionState)
    proxy_ref: str | None = None
    """Имя прокси, с которым identity жила в прошлый раз (`ProxyLease.proxy_id`: `Proxy.id` или `Proxy.label`)."""
    fingerprint: Mapping[str, Any] | None = field(default=None, repr=False)
    """Отпечаток, который identity предъявляет сайту, — непрозрачен для пула."""
    user_data: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    """Данные site SDK, которые должны пережить процесс."""
    updated_at: datetime = field(default_factory=utc_now)
    version: int = 0
    """Версия, прочитанная из хранилища; 0 — записи ещё нет."""

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None:
            msg = "updated_at записи без таймзоны: неизвестно, чей это момент"
            raise ValueError(msg)
        object.__setattr__(self, "user_data", MappingProxyType(dict(self.user_data)))
        if self.fingerprint is not None:
            object.__setattr__(self, "fingerprint", MappingProxyType(dict(self.fingerprint)))


def dump_record(record: IdentityRecord) -> dict[str, Any]:
    """Запись как простые данные JSON."""
    state = record.state
    return {
        "key": record.key,
        "version": record.version,
        "updated_at": record.updated_at.isoformat(),
        "proxy_ref": record.proxy_ref,
        "fingerprint": dict(record.fingerprint) if record.fingerprint is not None else None,
        "user_data": dict(record.user_data),
        "state": {
            "cookies": [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires.isoformat() if cookie.expires is not None else None,
                    "secure": cookie.secure,
                    "http_only": cookie.http_only,
                    "same_site": cookie.same_site,
                }
                for cookie in state.cookies
            ],
            "origins": [
                {
                    "origin": origin.origin,
                    "local_storage": [list(pair) for pair in origin.local_storage],
                }
                for origin in state.origins
            ],
            "extras": dict(state.extras),
        },
    }


def load_record(data: Mapping[str, Any]) -> IdentityRecord:
    """Запись из простых данных — обратное `dump_record`."""
    state = cast("Mapping[str, Any]", data.get("state") or {})
    fingerprint = data.get("fingerprint")
    return IdentityRecord(
        key=str(data["key"]),
        version=int(data.get("version", 0)),
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
        proxy_ref=cast("str | None", data.get("proxy_ref")),
        fingerprint=cast("Mapping[str, Any]", fingerprint) if fingerprint is not None else None,
        user_data=cast("Mapping[str, Any]", data.get("user_data") or {}),
        state=SessionState(
            cookies=tuple(
                Cookie(
                    name=str(cookie["name"]),
                    value=str(cookie["value"]),
                    domain=str(cookie["domain"]),
                    path=str(cookie.get("path", "/")),
                    expires=(
                        datetime.fromisoformat(str(cookie["expires"]))
                        if cookie.get("expires")
                        else None
                    ),
                    secure=bool(cookie.get("secure", False)),
                    http_only=bool(cookie.get("http_only", False)),
                    same_site=cast("SameSite | None", cookie.get("same_site")),
                )
                for cookie in cast("list[Mapping[str, Any]]", state.get("cookies") or [])
            ),
            origins=tuple(
                Origin(
                    origin=str(origin["origin"]),
                    local_storage=tuple(
                        (str(pair[0]), str(pair[1]))
                        for pair in cast("list[list[Any]]", origin.get("local_storage") or [])
                    ),
                )
                for origin in cast("list[Mapping[str, Any]]", state.get("origins") or [])
            ),
            extras=cast("Mapping[str, Any]", state.get("extras") or {}),
        ),
    )


__all__ = ["IdentityRecord", "StaleRecordError", "dump_record", "load_record"]
