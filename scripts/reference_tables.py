"""Справочники событий и ошибок (`docs/reference/events.md`, `errors.md`) — из самого кода.

События — классы `browser_pool.events` по группам, как они разбиты в исходнике комментариями
`# --- группа ---`: что случилось (докстринг класса) и поля. Ошибки — виды сбоев `ErrorKind` и
исключения `browser_pool.errors`: чем является, когда бросается, какие данные несёт.

    uv run python scripts/reference_tables.py          # перезаписать обе страницы
    uv run python scripts/reference_tables.py --check  # код выхода 1, если страницы устарели
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "src" / "browser_pool"
TARGETS = ROOT / "docs" / "reference"
GROUP = re.compile(r"^# --- (?P<title>.+?) -+$")

EVENTS_HEADER = """# События

Страница сгенерирована из `src/browser_pool/events.py` скриптом `scripts/reference_tables.py`, руками её не правят.

Подписка идёт через `pool.on(EventType, handler)`; подписка на `PoolEvent` получает все события. У каждого события
есть поле `at`, момент в UTC. В полях только несекретное: ключи identity, номера аренд, браузеры, виды сбоев, имена
типов исключений. Как пользоваться событиями, описано на странице [Наблюдение](../guide/observability.md).
"""

ERRORS_HEADER = """# Ошибки и виды сбоев

Страница сгенерирована из `src/browser_pool/errors.py` скриптом `scripts/reference_tables.py`, руками её не правят.

Исключения пула наследуют `PoolError`: `except PoolError` ловит всё, что бросает пул, и ничего чужого. Вид сбоя
(`ErrorKind`) отвечает на другой вопрос: что сломалось в коде приложения посреди аренды и как пулу на это
реагировать. Подробнее на странице [Сбои и повторы](../guide/recovery.md).
"""


def _text(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def _summary(node: ast.ClassDef) -> str:
    """Первый абзац докстринга класса одной строкой."""
    doc = ast.get_docstring(node) or ""
    return _text(doc.split("\n\n", maxsplit=1)[0])


def _docstring_after(body: list[ast.stmt], index: int) -> str:
    following = body[index + 1] if index + 1 < len(body) else None
    if (
        isinstance(following, ast.Expr)
        and isinstance(following.value, ast.Constant)
        and isinstance(following.value.value, str)
    ):
        return _text(following.value.value)
    return ""


def _attributes(node: ast.ClassDef) -> list[tuple[str, str, str]]:
    """Аннотированные поля класса: имя, тип, пояснение под полем."""
    found: list[tuple[str, str, str]] = []
    for index, statement in enumerate(node.body):
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        name = statement.target.id
        annotation = ast.unparse(statement.annotation)
        if name.startswith("_") or annotation.startswith("ClassVar"):
            continue
        found.append((name, annotation, _docstring_after(node.body, index)))
    return found


def _fields_cell(node: ast.ClassDef) -> str:
    parts: list[str] = []
    for name, annotation, note in _attributes(node):
        item = f"`{name}` (`{_text(annotation)}`)"
        parts.append(f"{item}: {note[0].lower()}{note[1:]}" if note else item)
    return "<br>".join(parts)


def _classes(tree: ast.Module) -> list[ast.ClassDef]:
    return [node for node in tree.body if isinstance(node, ast.ClassDef)]


def _public(tree: ast.Module) -> set[str]:
    """Имена из `__all__` модуля."""
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
        ):
            return {str(item) for item in ast.literal_eval(node.value)}
    msg = "в модуле нет __all__"
    raise ValueError(msg)


def render_events() -> str:
    """Текст справочника событий."""
    source = (SOURCES / "events.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    public = _public(tree)
    groups = [
        (number, match["title"])
        for number, line in enumerate(source.splitlines(), start=1)
        if (match := GROUP.match(line))
    ]
    sections: dict[str, list[str]] = {}
    for node in _classes(tree):
        if node.name not in public or node.name == "PoolEvent":
            continue
        title = next((name for line, name in reversed(groups) if line < node.lineno), "общее")
        row = f"| `{node.name}` | {_summary(node)} | {_fields_cell(node)} |"
        sections.setdefault(title, []).append(row)
    parts = [EVENTS_HEADER]
    for title, rows in sections.items():
        heading = title[0].upper() + title[1:]
        table = ["| Событие | Что случилось | Поля |", "|---|---|---|", *rows]
        parts.append(f"## {heading}\n\n" + "\n".join(table) + "\n")
    return "\n".join(parts)


def _kinds(node: ast.ClassDef) -> list[str]:
    rows: list[str] = []
    for index, statement in enumerate(node.body):
        if isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name):
            name = statement.targets[0].id
            rows.append(f"| `{name}` | {_docstring_after(node.body, index)} |")
    return rows


def render_errors() -> str:
    """Текст справочника ошибок."""
    tree = ast.parse((SOURCES / "errors.py").read_text(encoding="utf-8"))
    classes = {node.name: node for node in _classes(tree)}
    kinds = ["| Вид | Что сломалось и что делает пул |", "|---|---|", *_kinds(classes["ErrorKind"])]
    rows = ["| Исключение | Основа | Когда | Данные |", "|---|---|---|---|"]
    for node in classes.values():
        bases = [ast.unparse(base) for base in node.bases]
        if node.name.startswith("_") or not any(
            base.endswith(("Error", "Signal")) for base in bases
        ):
            continue
        shown = (
            ", ".join(f"`{base}`" for base in bases if not base.startswith("_")) or "`Exception`"
        )
        rows.append(f"| `{node.name}` | {shown} | {_summary(node)} | {_fields_cell(node)} |")
    return "\n".join(
        [
            ERRORS_HEADER,
            "## Виды сбоев\n",
            "\n".join(kinds) + "\n",
            "## Исключения\n",
            "\n".join(rows) + "\n",
        ]
    )


def main() -> int:
    """Перезаписать справочники или (`--check`) проверить, что они актуальны."""
    pages = {TARGETS / "events.md": render_events(), TARGETS / "errors.md": render_errors()}
    if "--check" in sys.argv[1:]:
        stale = [
            path.relative_to(ROOT).as_posix()
            for path, text in pages.items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]
        if stale:
            print(f"{', '.join(stale)}: устарело — uv run python scripts/reference_tables.py")
            return 1
        print("events.md, errors.md: актуальны")
        return 0
    for path, text in pages.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"записан {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
