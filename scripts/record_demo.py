"""Ролик для документации: запустить демо и записать экран.

    uv run python scripts/record_demo.py windows-before
    uv run python scripts/record_demo.py windows-after --seconds 10

Пишет в `docs/_static/demo/` три файла: `<имя>.webm`, `<имя>.mp4` и постер `<имя>.png`.
Экран записывает ffmpeg (`gdigrab`), поэтому скрипт работает только на Windows и требует ffmpeg в
`PATH`. В кадр попадает весь рабочий стол: перед записью уберите с экрана всё лишнее.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "_static" / "demo"
FRAME_RATE = 15
WIDTH = 1200
STARTUP = 1.5
"""Сколько секунд ffmpeg нужно, чтобы начать запись, прежде чем стартует демо."""

DEMOS: dict[str, list[str]] = {
    "windows-before": ["-m", "examples.demos.windows", "--mode", "before"],
    "windows-after": ["-m", "examples.demos.windows", "--mode", "after"],
    "login-before": ["-m", "examples.demos.login", "--mode", "before"],
    "login-after": ["-m", "examples.demos.login", "--mode", "after"],
    "crash-recovery": ["-m", "examples.accounts.app", "--headed"],
}


def _run(command: list[str]) -> None:
    finished = subprocess.run(command, cwd=ROOT, check=False)  # noqa: S603
    if finished.returncode != 0:
        msg = f"команда завершилась с кодом {finished.returncode}: {' '.join(command)}"
        raise RuntimeError(msg)


def record(name: str, seconds: float) -> list[Path]:
    """Записать демо `name` и вернуть пути готовых файлов."""
    ffmpeg = shutil.which("ffmpeg")
    if sys.platform != "win32" or ffmpeg is None:
        msg = "запись экрана идёт через ffmpeg gdigrab: нужны Windows и ffmpeg в PATH"
        raise RuntimeError(msg)
    demo = [sys.executable, *DEMOS[name]]
    if name.startswith(("windows-", "login-")):
        demo += ["--hold", str(seconds)]
    TARGET.mkdir(parents=True, exist_ok=True)
    webm, mp4, poster = (TARGET / f"{name}{suffix}" for suffix in (".webm", ".mp4", ".png"))
    scale = f"scale={WIDTH}:-2"
    with tempfile.TemporaryDirectory() as scratch:
        raw = Path(scratch) / "raw.mkv"
        capture = [ffmpeg, "-y", "-loglevel", "error", "-f", "gdigrab"]
        capture += ["-framerate", str(FRAME_RATE), "-i", "desktop"]
        capture += [
            "-t",
            str(seconds + STARTUP),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(raw),
        ]
        recorder = subprocess.Popen(capture, cwd=ROOT)  # noqa: S603
        time.sleep(STARTUP)  # noqa: TID251 — скрипт записи, цикла событий нет
        try:
            _run(demo)
        finally:
            recorder.wait()
        if recorder.returncode != 0 or not raw.exists():
            msg = f"ffmpeg не записал экран (код {recorder.returncode})"
            raise RuntimeError(msg)
        quiet = [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw)]
        _run(
            [
                *quiet,
                "-vf",
                scale,
                "-c:v",
                "libvpx-vp9",
                "-b:v",
                "0",
                "-crf",
                "36",
                "-an",
                str(webm),
            ]
        )
        _run([*quiet, "-vf", scale, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(mp4)])
        still = [ffmpeg, "-y", "-loglevel", "error", "-ss", str(seconds * 0.8), "-i", str(raw)]
        _run([*still, "-vf", scale, "-frames:v", "1", str(poster)])
    return [webm, mp4, poster]


def main() -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description="записать ролик демо для документации")
    parser.add_argument("demo", choices=sorted(DEMOS))
    parser.add_argument("--seconds", type=float, default=8.0, help="длина ролика")
    options = parser.parse_args()
    for path in record(options.demo, options.seconds):
        size = path.stat().st_size / 1024
        print(f"{path.relative_to(ROOT).as_posix()}: {size:.0f} КБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
