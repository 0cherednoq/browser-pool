"""Страж процессов: дерево, сироты прошлого запуска, переиспользование PID, добивание пулом."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import override

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Limits, Topology
from browser_pool.driver import LaunchSpec
from browser_pool.events import OrphansReaped, PoolEvent
from browser_pool.procguard import (
    ProcessGuard,
    default_registry,
    kill_tree,
    parse_proc_stat,
    process_token,
)
from browser_pool.testing import FakeBrowser, FakeDriver

SLEEPER = "import time; time.sleep(120)"
# Родитель запускает ребёнка, печатает его pid и спит сам: дерево из двух процессов.
PARENT = (
    "import subprocess, sys, time;"
    f"child = subprocess.Popen([sys.executable, '-c', {SLEEPER!r}]);"
    "print(child.pid, flush=True); time.sleep(120)"
)


def wait_dead(pid: int, *, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process_token(pid) is None:
            return True
        time.sleep(0.05)
    return process_token(pid) is None


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen[str]]]:
    """Запущенные тестом процессы; всё, что выжило, добивается."""
    started: list[subprocess.Popen[str]] = []
    yield started
    for process in started:
        if process.poll() is None:  # умерших не добиваем: taskkill на каждого — секунды
            kill_tree(process.pid)
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()


def sleeper(processes: list[subprocess.Popen[str]], code: str = SLEEPER) -> subprocess.Popen[str]:
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    processes.append(process)
    return process


def write_registry(directory: Path, *, owner: Mapping[str, object], pids: list[int]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pool-1-dead.json"
    path.write_text(
        json.dumps(
            {
                "owner": owner,
                "processes": [{"pid": pid, "token": process_token(pid)} for pid in pids],
            }
        ),
        encoding="utf-8",
    )
    return path


# --- процессы --------------------------------------------------------------------------


def test_process_token_tells_alive_from_gone(processes: list[subprocess.Popen[str]]) -> None:
    process = sleeper(processes, "pass")
    process.wait(timeout=10)

    assert process_token(process.pid) is None
    assert process_token(os.getpid()) is not None
    assert process_token(-1) is None


def test_kill_tree_takes_the_children_too(processes: list[subprocess.Popen[str]]) -> None:
    parent = sleeper(processes, PARENT)
    assert parent.stdout is not None
    child = int(parent.stdout.readline())
    assert process_token(child) is not None

    kill_tree(parent.pid)

    assert wait_dead(parent.pid)
    assert wait_dead(child)


# --- сироты ----------------------------------------------------------------------------


async def test_orphans_of_a_dead_pool_are_reaped(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    orphan = sleeper(processes)
    registry = write_registry(tmp_path, owner={"pid": 999_999, "token": "умер"}, pids=[orphan.pid])

    reaped = await ProcessGuard(tmp_path).reap_orphans()

    assert reaped == 1
    assert wait_dead(orphan.pid)
    assert not registry.exists()


async def test_browsers_of_a_living_pool_are_left_alone(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    process = sleeper(processes)
    owner = {"pid": os.getpid(), "token": process_token(os.getpid())}
    registry = write_registry(tmp_path, owner=owner, pids=[process.pid])

    assert await ProcessGuard(tmp_path).reap_orphans() == 0
    assert process_token(process.pid) is not None
    assert registry.exists()


async def test_reused_pid_is_not_killed(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    stranger = sleeper(processes)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "pool-1-dead.json").write_text(
        json.dumps(
            {
                "owner": {"pid": 999_999, "token": "умер"},
                # Номер тот же, а процесс другой: отпечаток прошлого браузера не совпадает.
                "processes": [{"pid": stranger.pid, "token": "не тот процесс"}],
            }
        ),
        encoding="utf-8",
    )

    assert await ProcessGuard(tmp_path).reap_orphans() == 0
    assert process_token(stranger.pid) is not None


async def test_broken_registry_is_dropped(tmp_path: Path) -> None:
    (tmp_path / "pool-1-broken.json").write_text("{не json", encoding="utf-8")

    assert await ProcessGuard(tmp_path).reap_orphans() == 0
    assert list(tmp_path.iterdir()) == []


async def test_registry_lives_only_while_something_is_tracked(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    guard = ProcessGuard(tmp_path)
    process = sleeper(processes)

    await guard.track(process.pid)
    assert guard.tracked == {process.pid}
    assert len(list(tmp_path.glob("pool-*.json"))) == 1

    kill_tree(process.pid)
    assert await guard.release(process.pid, grace=5.0) is False  # уже умер сам
    assert list(tmp_path.glob("pool-*.json")) == []


async def test_concurrent_tracks_and_releases_keep_the_registry_whole(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    guard = ProcessGuard(tmp_path)
    started = [sleeper(processes) for _ in range(12)]
    pids = [process.pid for process in started]

    await asyncio.gather(*(guard.track(pid) for pid in pids))  # как запуск браузеров разом

    (registry,) = tmp_path.glob("pool-*.json")
    recorded = {entry["pid"] for entry in json.loads(registry.read_text("utf-8"))["processes"]}
    assert recorded == set(pids)

    for process in started:
        process.kill()
    killed = await asyncio.gather(*(guard.release(pid, grace=5.0) for pid in pids))

    assert killed == [False] * 12
    assert list(tmp_path.iterdir()) == []  # ни реестра, ни временных файлов


async def test_process_stays_in_the_registry_until_it_is_gone(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    guard = ProcessGuard(tmp_path)
    process = sleeper(processes)
    await guard.track(process.pid)

    releasing = asyncio.create_task(guard.release(process.pid, grace=30.0))
    await asyncio.sleep(
        1.0
    )  # процесс ещё жив: упади приложение сейчас, сироту найдёт следующий запуск

    (registry,) = tmp_path.glob("pool-*.json")
    assert str(process.pid) in registry.read_text("utf-8")
    process.kill()
    assert await releasing is False
    assert list(tmp_path.glob("pool-*.json")) == []


async def test_registry_write_failure_does_not_fail_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def denied(self: Path, target: Path) -> Path:
        _ = self, target
        msg = "файл занят другим процессом"
        raise PermissionError(msg)

    monkeypatch.setattr(Path, "replace", denied)
    guard = ProcessGuard(tmp_path)

    await guard.track(os.getpid())  # реестр не записался — страховка слабее, но запуск цел

    assert guard.tracked == {os.getpid()}
    assert "реестр" in caplog.text.lower()
    assert list(tmp_path.iterdir()) == []
    guard.close()


async def test_foreign_registry_that_cannot_be_removed_does_not_fail_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_registry(tmp_path, owner={"pid": 999_999, "token": "умер"}, pids=[])
    unlink = Path.unlink

    def denied(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith("pool-1-"):
            msg = "чужой файл"
            raise PermissionError(msg)
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied)

    assert await ProcessGuard(tmp_path).reap_orphans() == 0


def test_default_registry_belongs_to_the_user() -> None:
    registry = default_registry()

    assert registry.name == "procguard"
    if sys.platform != "win32":  # /tmp общий для всех пользователей хоста, каталог реестра — свой
        assert str(os.getuid()) in registry.parent.name


@pytest.mark.parametrize(
    ("stat", "expected"),
    [
        ("4242 (chrome) S 1 4242 4242 0 -1 4194560 " + "0 " * 12 + "777 0", ("S", 1, "777")),
        ("4242 (we (ird) name) Z 17 4242 4242 0 -1 4194560 " + "0 " * 12 + "9 0", ("Z", 17, "9")),
        ("4242 (chrome) S 1", None),
        ("", None),
    ],
    ids=["running", "zombie-with-brackets", "short", "empty"],
)
def test_proc_stat_is_parsed(stat: str, expected: tuple[str, int, str] | None) -> None:
    assert parse_proc_stat(stat) == expected


# --- в пуле ----------------------------------------------------------------------------


class ProcessBackedDriver(FakeDriver):
    """Фейк, за каждым браузером которого стоит настоящий процесс, не умирающий при закрытии."""

    def __init__(self, processes: list[subprocess.Popen[str]]) -> None:
        super().__init__()
        self.processes = processes
        self.pids: dict[int, int] = {}

    @override
    async def launch(self, spec: LaunchSpec) -> FakeBrowser:
        browser = await super().launch(spec)
        self.pids[browser.id] = sleeper(self.processes).pid
        return browser

    @override
    def pid(self, browser: FakeBrowser) -> int | None:
        return self.pids.get(browser.id)


async def test_pool_kills_a_browser_process_that_outlived_close(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    driver = ProcessBackedDriver(processes)
    pool = BrowserPool(
        driver,
        config=PoolConfig(topology=Topology(browsers=1), limits=Limits(spawn_delay=0.0)),
        process_guard=ProcessGuard(tmp_path),
    )

    async with pool, pool.page(Identity(key="mail:a")):
        (pid,) = driver.pids.values()
        assert len(list(tmp_path.glob("pool-*.json"))) == 1

    assert wait_dead(pid)
    assert list(tmp_path.glob("pool-*.json")) == []


async def test_pool_with_many_browsers_stops_cleanly(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    for _ in range(2):  # браузеры закрываются разом — и разом пишут реестр
        driver = ProcessBackedDriver(processes)
        pool = BrowserPool(
            driver,
            config=PoolConfig(
                topology=Topology(browsers=6, min_browsers=6), limits=Limits(spawn_delay=0.0)
            ),
            process_guard=ProcessGuard(tmp_path),
        )

        await pool.start()
        assert len(driver.pids) == 6
        await pool.stop()

        assert all(wait_dead(pid) for pid in driver.pids.values())
        assert list(tmp_path.iterdir()) == []


async def test_pool_start_reaps_orphans_and_says_so(
    tmp_path: Path, processes: list[subprocess.Popen[str]]
) -> None:
    orphan = sleeper(processes)
    write_registry(tmp_path, owner={"pid": 999_999, "token": "умер"}, pids=[orphan.pid])
    events: list[PoolEvent] = []
    pool = BrowserPool(FakeDriver(), process_guard=ProcessGuard(tmp_path))
    pool.on(OrphansReaped, events.append)

    async with pool:
        pass

    assert events == [OrphansReaped(count=1, at=events[0].at)]
    assert wait_dead(orphan.pid)


# --- настоящий браузер: kill -9 процесса приложения ------------------------------------

APP = """
import asyncio, sys
from pathlib import Path
from browser_pool import BrowserPool, Identity
from browser_pool.procguard import ProcessGuard
from browser_pool.drivers.playwright import PlaywrightDriver

async def main():
    driver = PlaywrightDriver()
    pool = BrowserPool(driver, process_guard=ProcessGuard(Path(sys.argv[1])))
    await pool.start()
    async with pool.page(Identity(key="mail:a")) as lease:
        print(driver.pid(lease.browser), flush=True)
        await asyncio.sleep(3600)

asyncio.run(main())
"""


@pytest.mark.browser
def test_killed_application_leaves_no_chromium(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    app = subprocess.Popen(
        [sys.executable, "-c", APP, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    assert app.stdout is not None
    chromium = int(app.stdout.readline())
    assert process_token(chromium) is not None

    app.kill()  # kill -9 / TerminateProcess: пул не успевает ничего закрыть
    app.wait(timeout=10)
    app.stdout.close()

    if not wait_dead(chromium, seconds=10):
        # Драйвер SDK не прибрал за собой — прибирает следующий старт пула.
        assert asyncio.run(ProcessGuard(tmp_path).reap_orphans()) >= 1
    assert wait_dead(chromium)
