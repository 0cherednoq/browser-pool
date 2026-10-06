"""Проверка на всех поддерживаемых версиях Python: `uv run poe matrix [3.12 3.13 ...]`.

Полный `poe check` идёт один раз — в основном `.venv`. Линтеры и типизатор от версии
интерпретатора не зависят: ruff и basedpyright и так целятся в нижнюю версию (3.12). На остальных
версиях — то, что зависит от интерпретатора:

- `test` — поведение, включая импорт каждого модуля (`tests/test_package.py`);
- `stdlib` — список модулей стандартной библиотеки свой у каждой версии: модуль, появившийся
  в 3.14, на 3.12 — сторонний пакет;
- `docs` — справочник конфига читает аннотации во время выполнения.

Окружение каждой версии — `.venv-3.X` рядом с `.venv` (uv создаёт его сам, по lock-файлу).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ("3.12", "3.13")
"""Версии кроме основной: основную проверяет полный `poe check` в `.venv`."""
PER_VERSION = ("stdlib", "docs", "test")
INHERITED = ("PYTHONHOME", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")
"""Окружение `.venv`, из которого запущен скрипт: с чужим `PYTHONHOME` интерпретатор 3.12 грузит
stdlib 3.14 и падает при старте, а `VIRTUAL_ENV` сбивает uv с окружения версии."""


def main(argv: list[str]) -> int:
    """Код выхода процесса: 0 — всё зелёное на всех версиях."""
    uv = shutil.which("uv")
    if uv is None:
        print("uv не найден в PATH: окружения версий создаёт uv")
        return 1
    versions = argv or list(VERSIONS)
    results = {"основная (.venv): check": _run([uv, "run", "poe", "check"])}
    for version in versions:
        location = f".venv-{version}"
        synced = _run(
            [uv, "sync", "--locked", "--all-extras", "--python", version],
            env={"UV_PROJECT_ENVIRONMENT": location},
        )
        if not synced:
            results[f"{version}: окружение"] = False
            continue
        poe = [uv, "run", "--no-sync", "poe", "-X", f"location={location}"]
        for task in PER_VERSION:
            results[f"{version}: {task}"] = _run(
                [*poe, task], env={"UV_PROJECT_ENVIRONMENT": location}
            )
    print("\nИтог матрицы:")
    for name, passed in results.items():
        print(f"  {'ок  ' if passed else 'СБОЙ'}  {name}")
    return 0 if all(results.values()) else 1


def _run(command: list[str], *, env: dict[str, str] | None = None) -> bool:
    print(f"\n$ {' '.join(command[1:])}", flush=True)
    inherited = {name: value for name, value in os.environ.items() if name not in INHERITED}
    environment = {**inherited, **(env or {})}
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode == 0  # noqa: S603


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
