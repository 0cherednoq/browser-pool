"""Приёмка M2/M3: сценарий `examples.accounts` на каждом драйвере — падение браузера без повторных входов."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from browser_pool.procguard import kill_tree

pytestmark = pytest.mark.browser

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 300
DUMP_AFTER = 180
"""Демо обычно идёт 30–40 с; дольше — печатает стеки задач и снимок пула (разбор зависаний)."""
PYDOLL_API = 3


@pytest.mark.parametrize(
    ("driver", "sdk"), [("playwright", "playwright.async_api"), ("pydoll", "pydoll")]
)
def test_demo_survives_a_killed_browser_without_new_logins(
    driver: str, sdk: str, tmp_path: Path
) -> None:
    pytest.importorskip(sdk)
    if driver == "pydoll" and int(version("pydoll-python").split(".")[0]) < PYDOLL_API:
        # Библиотека работает и с pydoll 2.27 (нижняя граница, CI `lowest`), а SDK почты примера
        # написан под API 3.x: там `current_url()` и `text()` — методы, а не свойства.
        pytest.skip(f"SDK почты примера — под pydoll {PYDOLL_API}.x")
    # Вывод — в файлы, а не в трубы: при зависании его можно прочитать, а трубу не держит
    # никакой потомок демо.
    out, err = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    command = [
        sys.executable,
        "-m",
        "examples.accounts.app",
        "--json",
        "--verbose",
        "--dump-after",
        str(DUMP_AFTER),
        "--accounts",
        "4",
        "--rounds",
        "3",
        "--driver",
        driver,
    ]
    # Логи демо — по-русски: кодировка консоли не должна решать, пройдёт ли тест.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with out.open("w", encoding="utf-8") as stdout, err.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr)
        try:
            process.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            kill_tree(process.pid)
            process.wait()
            log = err.read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"демо не закончилось за {TIMEOUT} с; stderr (хвост):\n{log[-20000:]}")

    log = err.read_text(encoding="utf-8", errors="replace")
    report = json.loads(out.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert report["passed"], log
    assert report["killed_pid"] is not None
    assert report["browser_restarts"] >= 1
    assert report["tasks_done"] == 4 * 3
    assert report["password_logins"] == {f"user{index}": 1 for index in range(4)}
    assert all(count > 0 for count in report["proxy_requests"].values())
    assert process.returncode == 0, log
