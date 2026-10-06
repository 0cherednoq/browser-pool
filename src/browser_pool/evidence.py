"""Улики сбоя: что было на вкладке, когда аренда упала.

Если пулу дан приёмник (`BrowserPool(evidence_sink=…)`), при исключении в аренде вкладки пул снимает
с неё снимок, разметку и адрес (`driver.capture`) — до того, как вкладку выбросит, — и отдаёт
приёмнику. Ссылку, которую вернул приёмник, несёт событие `LeaseReleased.evidence`. Сбой съёмки
или приёмника — предупреждение в лог: улики не важнее аренды.

Улики — данные пользователя (разметка страницы, скриншот): в лог и события идёт только ссылка.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from browser_pool._filename import safe_file_name
from browser_pool.clock import utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from browser_pool.driver import Evidence


@runtime_checkable
class EvidenceSink(Protocol):
    """Куда складывать улики сбоев."""

    async def save(self, evidence: Evidence, *, key: str, lease_id: int, error: str) -> str:
        """Сохранить улики аренды `lease_id` identity `key` (`error` — имя типа исключения).

        Возвращает ссылку — путь, идентификатор в хранилище, — которая попадёт в событие.
        """
        ...


class DirectoryEvidenceSink:
    """Улики — файлами в каталог: `<время>-<identity>-<аренда>.png|.jpg|.webp|.html|.json`.

    Имя identity — читаемая часть ключа и хеш ключа (`mail:a` и `mail_a` — разные), точки и двоеточия
    в ключе номер аренды не съедают. Ссылка, которую возвращает `save`, — путь к файлу метаданных
    `.json`: он есть всегда; остальные файлы комплекта лежат рядом под тем же именем.

    Формат снимка — какой отдал SDK (pydoll снимает JPEG, Playwright — PNG): расширение — по
    сигнатуре файла.

    `keep` — сколько последних комплектов хранить; `None` — все.
    """

    def __init__(self, root: Path, *, keep: int | None = 200) -> None:
        """`root` создаётся при первой записи."""
        if keep is not None and keep < 1:
            msg = f"keep должен быть ≥ 1, получено {keep}"
            raise ValueError(msg)
        self._root = root
        self._keep = keep

    async def save(self, evidence: Evidence, *, key: str, lease_id: int, error: str) -> str:
        """Записать комплект в потоке исполнителя; вернуть путь к файлу метаданных."""
        at = utc_now()
        stem = f"{at:%Y%m%d-%H%M%S-%f}-{safe_file_name(key)}-{lease_id}"
        meta = {
            "key": key,
            "lease_id": lease_id,
            "error": error,
            "url": evidence.url,
            "at": at.isoformat(),
        }
        return await asyncio.to_thread(self._write, stem, evidence, meta)

    def _write(self, stem: str, evidence: Evidence, meta: Mapping[str, object]) -> str:
        self._root.mkdir(parents=True, exist_ok=True)
        if evidence.screenshot is not None:
            self._file(stem, image_suffix(evidence.screenshot)).write_bytes(evidence.screenshot)
        if evidence.html is not None:
            self._file(stem, ".html").write_text(evidence.html, encoding="utf-8")
        reference = self._file(stem, ".json")
        reference.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self._prune()
        return str(reference)

    def _file(self, stem: str, suffix: str) -> Path:
        # Расширение добавляется к имени, а не заменяет его хвост: в имени бывают точки (`ada@example.com`).
        return self._root / f"{stem}{suffix}"

    def _prune(self) -> None:
        """Оставить `keep` последних комплектов (по метаданным: они пишутся всегда)."""
        if self._keep is None:
            return
        sets = sorted(self._root.glob("*.json"))
        for meta in sets[: max(0, len(sets) - self._keep)]:
            stem = meta.name.removesuffix(".json")
            for suffix in (*_IMAGE_SUFFIXES, ".html", ".json"):
                self._file(stem, suffix).unlink(missing_ok=True)


_SIGNATURES = ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"))
_IMAGE_SUFFIXES = (".png", ".jpg", ".webp", ".img")


def image_suffix(data: bytes) -> str:
    """Расширение снимка по сигнатуре: PNG, JPEG, WebP; неизвестный формат — `.img`."""
    for signature, suffix in _SIGNATURES:
        if data.startswith(signature):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".img"


__all__ = ["DirectoryEvidenceSink", "EvidenceSink", "image_suffix"]
