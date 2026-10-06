"""Колесо как у пользователя: чистое окружение, установка без экстр, импорт и аренда на фейке.

    uv build && uv run python scripts/smoke_wheel.py dist/browser_pool-*.whl --python 3.12

Проверяет то, чего не видят тесты в окружении разработки: колесо ставится на заявленный Python,
в нём есть всё нужное (`py.typed`, тестовый набор), ядро не тянет SDK браузеров, версия пакета —
та, что в имени колеса, и пул работает. Нужен `uv` в PATH; сам скрипт — только stdlib.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SMOKE = """
import asyncio, sys
from importlib.resources import files

import browser_pool
from browser_pool import BrowserPool, Identity
from browser_pool.testing import FakeDriver

assert browser_pool.__version__ == sys.argv[1], (browser_pool.__version__, sys.argv[1])
assert files("browser_pool").joinpath("py.typed").is_file(), "нет py.typed"
sdks = {"playwright", "pydoll"} & set(sys.modules)
assert not sdks, f"импорт ядра тянет SDK: {sdks}"

async def main():
    async with BrowserPool(FakeDriver()) as pool, pool.page(Identity(key="smoke")) as lease:
        assert lease.page.alive

asyncio.run(main())
print(f"ok: browser-pool {browser_pool.__version__} на Python {sys.version.split()[0]}")
"""


def main() -> int:
    """Код выхода процесса: 0 — колесо ставится и работает."""
    parser = argparse.ArgumentParser(description="Проверка колеса в чистом окружении")
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--python", default="3.12", help="версия Python для окружения")
    options = parser.parse_args()
    wheel: Path = options.wheel.resolve()
    version = wheel.name.split("-")[1]
    uv = shutil.which("uv")
    if uv is None:
        print("uv не найден в PATH")
        return 1
    # Окружение разработки не должно просочиться: ни его venv, ни его PYTHONHOME.
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"PYTHONHOME", "VIRTUAL_ENV", "PYTHONPATH"}
    }
    with tempfile.TemporaryDirectory(prefix="smoke-") as root:
        venv = Path(root) / "venv"
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        steps = [
            [uv, "venv", "--quiet", "--python", options.python, str(venv)],
            [uv, "pip", "install", "--quiet", "--python", str(python), str(wheel)],
            [str(python), "-c", SMOKE, version],
        ]
        for step in steps:
            # Запуск из временного каталога: иначе `import browser_pool` нашёл бы исходники рядом.
            finished = subprocess.run(step, cwd=root, env=env, check=False)  # noqa: S603
            if finished.returncode != 0:
                print(f"не прошло: {' '.join(step[:3])}")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
