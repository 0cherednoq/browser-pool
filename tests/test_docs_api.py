"""Справочник API на сайте полон: всё публичное описано в `docs/reference/api`.

Страницы справочника подключают модули директивами `automodule` и `autoclass`. Тест читает эти
директивы и проверяет, что каждое имя из `__all__` публичных пакетов и модулей (README, «Публичный
API») попадает хотя бы в одну из них. Новое публичное имя без страницы в справочнике — ошибка.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = sorted((ROOT / "docs" / "reference" / "api").glob("*.md"))
DIRECTIVE = re.compile(r"^\.\. auto(module|class):: ([\w.]+)$", re.MULTILINE)

PUBLIC = (
    "browser_pool",
    "browser_pool.providers",
    "browser_pool.proxies",
    "browser_pool.state",
    "browser_pool.events",
    "browser_pool.testing",
    "browser_pool.driver",
    "browser_pool.provider",
    "browser_pool.flow",
    "browser_pool.hooks",
    "browser_pool.config",
    "browser_pool.errors",
    "browser_pool.snapshot",
    "browser_pool.geometry",
)

# Публичные имена, которым в справочнике места нет: внутренние значения, попавшие в `__all__` ради тестов.
UNDOCUMENTED: frozenset[str] = frozenset()


def _public_names(module: object) -> list[str]:
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return list(declared)
    return [name for name in vars(module) if not name.startswith("_")]


def documented() -> tuple[set[int], set[str]]:
    """Объекты и имена, которые описывают директивы справочника."""
    objects: set[int] = set()
    names: set[str] = set()
    for page in PAGES:
        for kind, target in DIRECTIVE.findall(page.read_text(encoding="utf-8")):
            if kind == "class":
                module_name, _, class_name = target.rpartition(".")
                objects.add(id(getattr(importlib.import_module(module_name), class_name)))
                names.add(class_name)
                continue
            module = importlib.import_module(target)
            for name in _public_names(module):
                objects.add(id(getattr(module, name)))
                names.add(name)
    return objects, names


def test_the_reference_has_pages() -> None:
    assert PAGES, "docs/reference/api пуст"


@pytest.mark.parametrize("module_name", PUBLIC)
def test_every_public_name_is_in_the_reference(module_name: str) -> None:
    objects, names = documented()
    module = importlib.import_module(module_name)

    missing = [
        name
        for name in _public_names(module)
        if id(getattr(module, name)) not in objects
        and name not in names
        and name not in UNDOCUMENTED
    ]

    assert not missing, f"В справочнике API нет имён из {module_name}: " + ", ".join(missing)
