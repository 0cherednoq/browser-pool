"""Имена в документации существуют в коде (M8.21): гайды, README и CHANGELOG не ссылаются на то, чего нет.

Проверяются имена в обратных кавычках вне блоков кода: токен, похожий на идентификатор из библиотеки
(`snake_case`, `CamelCase` или цепочка через точку), должен встречаться в `src/browser_pool` как имя
класса, функции, аргумента, атрибута или поля. Путь `browser_pool.…` — модуль или атрибут, который
можно импортировать. Имена чужих SDK, стандартной библиотеки и значения `Literal` — в `ALLOWED`.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "browser_pool"
# Страницы сайта — все `.md` в подкаталогах `docs/`; рабочие документы лежат в самом `docs/` и сюда не входят.
SITE = sorted(
    path
    for path in (ROOT / "docs").glob("*/**/*.md")
    if "_build" not in path.parts and "raw" not in path.parts
)
DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "docs" / "index.md", *SITE]

ALLOWED = frozenset(
    {
        # чужие SDK и стандартная библиотека
        "BrowserContext",
        "ValueError",
        "TimeoutError",
        "CancelledError",
        "__cause__",
        "__context__",
        "add_note",
        "asyncio",
        "timeout",
        "CHANGELOG",
        "md",
        "auth.json",
        "cookies.txt",
        # метки pytest и режимы
        "virtual_time",
        # приложения-примеры из examples/
        "mail_sdk",
        "quotes_sdk",
        "QuotesFlow",
        "quotes_on_page",
        # значения Literal-полей и режимов
        "launch_only",
        "playwright_ws",
        "per_context",
        "per_page",
        "read_write",
        "read_only",
        "round_robin",
        "least_used",
        "rate_limited",
        "minimize_idle",
        "pre_commit",
    }
)

TOKEN = re.compile(r"`([^`\n]+)`")
REFERENCE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*(\(\))?")


def _names_in(node: ast.AST) -> set[str]:
    """Имена, которые вводит один узел дерева."""
    match node:
        case ast.ClassDef() | ast.TypeAlias():
            name = node.name if isinstance(node, ast.ClassDef) else getattr(node.name, "id", "")
            return {name}
        case ast.FunctionDef() | ast.AsyncFunctionDef():
            arguments = node.args
            given = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
            return {node.name, *(item.arg for item in given)}
        case ast.AnnAssign(target=ast.Name() as target):
            return {target.id}
        case ast.Assign(targets=targets):
            return {target.id for target in targets if isinstance(target, ast.Name)}
        case ast.Attribute(ctx=ast.Store()):
            return {node.attr}
        case _:
            return set()


def defined_names() -> set[str]:
    names: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names |= _names_in(node)
    return names


def references(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    in_code = False
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for match in TOKEN.finditer(line):
            token = match.group(1)
            if not REFERENCE.fullmatch(token):
                continue
            token = token.removesuffix("()")
            looks_like_code = "." in token or "_" in token or re.match(r"[A-Z][a-z]+[A-Z]", token)
            if looks_like_code:
                found.append((number, token))
    return found


def importable(dotted: str) -> bool:
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        target: object = module
        for attribute in parts[split:]:
            if not hasattr(target, attribute):
                return False
            target = getattr(target, attribute)
        return True
    return False


KNOWN = defined_names()


def missing_names(path: Path) -> list[str]:
    """Имена из `path`, которых нет в коде, в виде «файл:строка `имя`»."""
    missing: list[str] = []
    for number, token in references(path):
        if token.startswith("browser_pool"):
            exists = importable(token)
        else:
            exists = token in ALLOWED or all(
                part in KNOWN or part in ALLOWED for part in token.split(".")
            )
        if not exists:
            missing.append(f"{path.name}:{number} `{token}`")
    return missing


@pytest.mark.parametrize("path", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_documented_names_exist_in_the_code(path: Path) -> None:
    missing = missing_names(path)

    assert not missing, "Имена из документации, которых нет в коде: " + ", ".join(missing)


def test_the_check_catches_a_reference_to_a_missing_config_field(tmp_path: Path) -> None:
    page = tmp_path / "guide.md"
    page.write_text(
        "Лимит — `Limits.max_waiting`, а ещё `Limits.no_such_field` и `Recovery.proxy_retries`.\n"
        "Модуль `browser_pool.no_such_module`, настоящий — `browser_pool.config`.\n",
        encoding="utf-8",
    )

    assert missing_names(page) == [
        "guide.md:1 `Limits.no_such_field`",
        "guide.md:2 `browser_pool.no_such_module`",
    ]


@pytest.mark.parametrize("path", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_install_command_asks_for_the_prerelease(path: Path) -> None:
    """Пока выпуск — альфа, `pip install browser-pool` без `--pre` не найдёт пакет."""
    commands = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if "pip install" in line and "browser-pool" in line
    ]

    assert all("--pre" in command for command in commands), commands
