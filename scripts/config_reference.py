"""Справочник конфига `docs/reference/config.md` — из самого кода, чтобы не расходился с ним.

Секции — поля `PoolConfig` по порядку, плюс вспомогательные значения (`GroupLimit`, `Backoff`).
Для каждого поля: тип (как в аннотации), значение по умолчанию (из dataclass) и описание —
докстринг под полем в `config.py`.

    uv run python scripts/config_reference.py          # перезаписать docs/reference/config.md
    uv run python scripts/config_reference.py --check  # код выхода 1, если файл устарел
"""

from __future__ import annotations

import ast
import dataclasses
import sys
from pathlib import Path
from typing import Any, cast

from browser_pool import config

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "browser_pool" / "config.py"
TARGET = ROOT / "docs" / "reference" / "config.md"
EXTRA = ("GroupLimit", "Backoff")

HEADER = """# Справочник конфига

Страница сгенерирована из `src/browser_pool/config.py` скриптом `scripts/config_reference.py`, руками её не правят.

Конфиг собирается кодом (`PoolConfig(topology=Topology(browsers=3))`) или из словаря
(`PoolConfig.from_mapping({"topology": {"browsers": 3}})`: TOML, YAML, env). На неизвестный ключ и неверный тип
приходит `ConfigError` со всеми проблемами разом. Секунды можно задать числом или `timedelta`, `None` означает
«выключено» или «без предела».
"""


def render() -> str:
    """Текст справочника."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    parts = [HEADER]
    for section in dataclasses.fields(config.PoolConfig):
        cls = _section_class(section)
        parts.append(_section(f"`{section.name}` - `{cls.__name__}`", cls, classes[cls.__name__]))
    parts.append("## Вспомогательные значения\n")
    for name in EXTRA:
        cls = getattr(config, name)
        parts.append(_section(f"`{name}`", cls, classes[name], level="###"))
    return "\n".join(parts)


def _section_class(section: dataclasses.Field[Any]) -> type:
    factory = section.default_factory
    if factory is dataclasses.MISSING:
        msg = f"секция {section.name} без default_factory"
        raise TypeError(msg)
    section_type: type = type(cast("object", factory()))
    return section_type


def _section(title: str, cls: type, node: ast.ClassDef, *, level: str = "##") -> str:
    doc = (ast.get_docstring(node) or "").strip()
    rows = ["| Поле | Тип | По умолчанию | Смысл |", "|---|---|---|---|"]
    defaults = {field.name: field for field in dataclasses.fields(cls)}
    for name, annotation, description in _fields(node):
        default = _cell(_default(defaults[name]))
        rows.append(f"| `{name}` | `{_cell(annotation)}` | {default} | {_cell(description)} |")
    return f"{level} {title}\n\n{doc}\n\n" + "\n".join(rows) + "\n"


def _fields(node: ast.ClassDef) -> list[tuple[str, str, str]]:
    """Поля класса по порядку: имя, аннотация, докстринг под ним."""
    found: list[tuple[str, str, str]] = []
    body = node.body
    for index, statement in enumerate(body):
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        following = body[index + 1] if index + 1 < len(body) else None
        description = ""
        if (
            isinstance(following, ast.Expr)
            and isinstance(following.value, ast.Constant)
            and isinstance(following.value.value, str)
        ):
            description = following.value.value
        found.append((statement.target.id, ast.unparse(statement.annotation), description))
    return found


def _default(field: dataclasses.Field[Any]) -> str:
    if field.default is not dataclasses.MISSING:
        value = field.default
    elif field.default_factory is not dataclasses.MISSING:
        value = field.default_factory()
    else:
        return "обязательно"
    return f"`{value!r}`"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def main() -> int:
    """Перезаписать справочник или (`--check`) проверить, что он актуален."""
    text = render()
    if "--check" in sys.argv[1:]:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET.relative_to(ROOT)} устарел: uv run python scripts/config_reference.py")
            return 1
        print("config.md: актуален")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"записан {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
