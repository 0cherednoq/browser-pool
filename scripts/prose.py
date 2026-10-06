"""Проверка прозы документации на машинный стиль: `uv run poe prose`.

Гоняет `scripts/vendor/prose_lint.py` по страницам сайта, README, CONTRIBUTING и CHANGELOG.
`ERROR` (длинное тире, математические знаки в прозе, негативные параллелизмы, рубленые фрагменты,
разделители, следы копирования из чат-бота) роняет задачу. `WARN` печатаются с ключом `--warnings`
и на код выхода не влияют: их оценивают глазами.

    uv run python scripts/prose.py              # только ошибки
    uv run python scripts/prose.py --warnings   # и предупреждения
    uv run python scripts/prose.py docs/guide/login.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINTER = ROOT / "scripts" / "vendor" / "prose_lint.py"


def pages() -> list[Path]:
    """Страницы сайта (подкаталоги `docs/`) и документы в корне репозитория."""
    site = sorted(
        path
        for path in (ROOT / "docs").glob("*/**/*.md")
        if "_build" not in path.parts and "raw" not in path.parts
    )
    top = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md")]
    return [*top, ROOT / "docs" / "index.md", *site]


def check(path: Path, *, warnings: bool) -> bool:
    """Проверить одну страницу; `True` — жёстких запретов нет."""
    finished = subprocess.run(  # noqa: S603
        [sys.executable, str(LINTER), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    wanted = ("ERROR", "WARN") if warnings else ("ERROR",)
    lines = [line for line in finished.stdout.splitlines() if line.startswith(wanted)]
    if lines:
        print(path.relative_to(ROOT).as_posix())
        for line in lines:
            print(f"  {line}")
    return finished.returncode == 0


def main() -> int:
    """Проверить названные страницы или все."""
    arguments = sys.argv[1:]
    warnings = "--warnings" in arguments
    named = [ROOT / name for name in arguments if not name.startswith("--")]
    targets = named or pages()
    failed = [path for path in targets if not check(path, warnings=warnings)]
    if failed:
        print(f"проза: жёсткие запреты в {len(failed)} из {len(targets)} страниц")
        return 1
    print(f"проза: {len(targets)} страниц без жёстких запретов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
