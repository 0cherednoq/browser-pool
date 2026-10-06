"""Ядро `browser_pool` импортирует только стандартную библиотеку.

import-linter умеет запретить перечисленные пакеты, но не «всё, кроме stdlib»: список
запретов всегда отстаёт от того, что кто-нибудь импортирует завтра. Здесь наоборот —
разрешено только stdlib и сам пакет, а исключения перечислены явно.

Проверяются все импорты, включая импорты внутри функций и под `TYPE_CHECKING`: тип из
стороннего пакета в публичной сигнатуре ядра так же обязывает потребителя его поставить.
Заодно проверяется, что у пакета нет обязательных runtime-зависимостей.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "browser_pool"

# Подпакеты, которым сторонние импорты разрешены, и что именно им разрешено.
# `None` — любые: адаптер обязан импортировать свой SDK (лениво — это держит ruff TID253).
EXEMPT: dict[str, frozenset[str] | None] = {
    "drivers": None,
    "providers": None,
    "monitors": frozenset({"psutil", "prometheus_client"}),
    "testing": frozenset({"pytest", "pytest_asyncio", "hypothesis"}),
}
# `_typeshed` — стабы stdlib для типизатора: импортируется только под TYPE_CHECKING, в рантайме его нет.
ALWAYS_ALLOWED = frozenset({"__future__", "browser_pool", "_typeshed"})
NOTHING: frozenset[str] = frozenset()


def main() -> int:
    """Код выхода процесса: 0 — нарушений нет."""
    problems = [*_dependency_problems(), *_import_problems()]
    for problem in problems:
        print(problem)
    if problems:
        print(f"\nНарушений: {len(problems)}. Ядро browser_pool — только stdlib.")
        return 1
    print("stdlib-only: ок")
    return 0


def _dependency_problems() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies: list[str] = project.get("dependencies", [])
    return [
        f"pyproject.toml: обязательная зависимость {name!r} — выносите в optional-dependencies"
        for name in dependencies
    ]


def _import_problems() -> list[str]:
    problems: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        exempt = EXEMPT.get(relative.parts[0], NOTHING) if len(relative.parts) > 1 else NOTHING
        if exempt is None:
            continue
        allowed = ALWAYS_ALLOWED | exempt
        for line, name in _imported_top_levels(path):
            if name not in allowed and name not in sys.stdlib_module_names:
                shown = path.relative_to(ROOT).as_posix()
                problems.append(f"{shown}:{line}: импорт стороннего пакета {name!r}")
    return problems


def _imported_top_levels(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name.partition(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            found.append((node.lineno, node.module.partition(".")[0]))
    return found


if __name__ == "__main__":
    sys.exit(main())
