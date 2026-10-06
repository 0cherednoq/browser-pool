"""Примеры работают как написано и устроены как настоящий проект (M5.06, M6.04).

Правило слоёв проверяется разбором импортов, а не обещанием в докстринге:

- SDK сайта (`*_sdk`) не знает о пуле — `browser_pool` не импортирует вовсе;
- никто в примерах не берёт внутренности пула (`browser_pool._core`) и приватные имена чужих модулей.
"""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Модули, которые импортирует файл, и имена, взятые из них."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append((node.module, tuple(alias.name for alias in node.names)))
    return found


def example_files() -> list[Path]:
    return sorted(EXAMPLES.rglob("*.py"))


def sdk_files() -> list[Path]:
    return [path for path in example_files() if any(p.endswith("_sdk") for p in path.parts)]


def test_there_are_site_sdks_to_check() -> None:
    assert {path.parent.name for path in sdk_files()} >= {"quotes_sdk", "mail_sdk"}


@pytest.mark.parametrize("path", sdk_files(), ids=lambda path: path.relative_to(ROOT).as_posix())
def test_site_sdk_does_not_know_the_pool(path: Path) -> None:
    modules = [module for module, _ in imports(path)]
    assert not [module for module in modules if module.split(".")[0] == "browser_pool"]


@pytest.mark.parametrize(
    "path", example_files(), ids=lambda path: path.relative_to(ROOT).as_posix()
)
def test_examples_use_only_public_names(path: Path) -> None:
    for module, names in imports(path):
        assert not module.startswith("browser_pool._core"), f"{module}: внутренности пула"
        assert not any(part.startswith("_") for part in module.split(".")[1:]), module
        assert not [name for name in names if name.startswith("_")], f"{module}: {names}"


def test_quickstart_runs() -> None:
    finished = subprocess.run(
        [sys.executable, "-m", "examples.quickstart"],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    assert "user0: привет" in finished.stdout
    assert "['user0: рассылка', 'user1: рассылка', 'user2: рассылка']" in finished.stdout
    assert "аренд сейчас 0, контекстов 3" in finished.stdout


def _online(host: str) -> bool:
    try:
        with socket.create_connection((host, 443), timeout=5):
            return True
    except OSError:
        return False


@pytest.mark.browser
@pytest.mark.parametrize("sessions", ["pool", "sdk"])
def test_quotes_app_on_the_live_site(sessions: str) -> None:
    pytest.importorskip("playwright.async_api")
    if not _online("quotes.toscrape.com"):
        pytest.skip("нет доступа к quotes.toscrape.com")
    finished = subprocess.run(
        [
            sys.executable,
            "-m",
            "examples.quotes.app",
            "--sessions",
            sessions,
            "--accounts",
            "2",
            "--pages",
            "3",
            "--parallel",
            "2",
        ],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert "ИТОГ: ВСЁ РАБОТАЕТ" in finished.stdout


START_OUTPUT = ROOT / "docs" / "tutorial" / "output"


@pytest.mark.browser
@pytest.mark.parametrize("name", ["minimal", "readers", "login"])
def test_start_examples_print_what_the_docs_show(name: str) -> None:
    """Страница «Начало работы» показывает вывод из файла: он обязан совпадать с настоящим."""
    pytest.importorskip("playwright.async_api")
    if not _online("quotes.toscrape.com"):
        pytest.skip("нет доступа к quotes.toscrape.com")
    finished = subprocess.run(
        [sys.executable, "-m", f"examples.start.{name}"],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert finished.stdout == (START_OUTPUT / f"{name}.txt").read_text(encoding="utf-8")
