"""Пакет как поставка: импортируется без SDK браузеров и объявляет себя типизированным."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.resources import files
from pathlib import Path

BROWSER_SDKS = ("playwright", "selenium", "camoufox", "pydoll", "nodriver", "zendriver")


def test_import_does_not_pull_browser_sdks() -> None:
    # Отдельный процесс: в этом pytest мог уже импортировать что угодно.
    probe = (
        "import sys, browser_pool; "
        f"print(','.join(name for name in {BROWSER_SDKS!r} if name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def test_every_module_imports() -> None:
    # Матрица версий (`poe matrix`) гоняет это на 3.12–3.14: синтаксис и аннотации новее
    # поддерживаемой версии падают здесь, даже если модуль не задет ни одним тестом.
    # SDK адаптеры импортируют лениво, так что модули импортируются и без экстр.
    probe = (
        "import importlib, pkgutil, browser_pool; "
        "[importlib.import_module(m.name) "
        "for m in pkgutil.walk_packages(browser_pool.__path__, 'browser_pool.')]"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_package_is_marked_typed() -> None:
    assert files("browser_pool").joinpath("py.typed").is_file()


def test_version_comes_from_the_project() -> None:
    import browser_pool

    project = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert browser_pool.__version__ == project["version"]
