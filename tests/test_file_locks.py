"""Файловые замки: один держатель на хост, смерть процесса освобождает замок."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from browser_pool.locks import FileIdentityLock, FileLock, safe_file_name

HOLDER = """
import asyncio, sys, time
from pathlib import Path
from browser_pool.locks import FileLock

async def main():
    await FileLock(Path(sys.argv[1])).acquire()
    print("locked", flush=True)
    time.sleep(60)

asyncio.run(main())
"""


async def taken_within(lock: FileLock, seconds: float) -> bool:
    try:
        async with asyncio.timeout(seconds):
            await lock.acquire()
    except TimeoutError:
        return False
    return True


async def taken_eventually(lock: FileLock, seconds: float) -> bool:
    """В настоящем времени: Windows снимает замок убитого процесса не мгновенно, а виртуальное
    время теста пролетело бы срок за миллисекунды."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        if await taken_within(lock, 0.1):
            return True
    return False


async def test_second_holder_waits_until_release(tmp_path: Path) -> None:
    path = tmp_path / "profile.lock"
    first, second = FileLock(path), FileLock(path)

    await first.acquire()
    assert first.held
    assert not await taken_within(second, 1.0)  # тот же процесс — тоже ждёт

    first.release()
    assert await taken_within(second, 1.0)
    second.release()
    assert path.exists()  # файл замка остаётся: удаление открыло бы гонку за имя


async def test_misuse_is_an_error(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "x.lock")
    with pytest.raises(RuntimeError, match="не взят"):
        lock.release()
    await lock.acquire()
    with pytest.raises(RuntimeError, match="уже взят"):
        await lock.acquire()
    lock.release()


async def test_cancelled_wait_leaves_no_lock_behind(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    holder = FileLock(path)
    await holder.acquire()
    waiter = FileLock(path)
    task = asyncio.create_task(waiter.acquire())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not waiter.held

    holder.release()
    assert await taken_within(FileLock(path), 1.0)


async def test_directory_is_created_on_first_attempt(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "a" / "b" / "x.lock")
    await lock.acquire()
    lock.release()


async def test_killed_holder_frees_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "profile.lock"
    process = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-c", HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = await asyncio.to_thread(process.stdout.readline) if process.stdout else ""
        assert line.strip() == "locked"
        lock = FileLock(path)
        assert not await taken_within(lock, 1.0)

        process.kill()  # как падение воркера: ни finally, ни release
        await asyncio.to_thread(process.wait)
        assert await taken_eventually(lock, 10.0)
        lock.release()
    finally:
        process.kill()
        await asyncio.to_thread(process.wait)
        if process.stdout is not None:
            process.stdout.close()


# --- замок identity на файлах -----------------------------------------------------------


async def test_file_identity_lock_excludes_across_instances(tmp_path: Path) -> None:
    first, second = FileIdentityLock(tmp_path), FileIdentityLock(tmp_path)

    await first.acquire("mail:a")
    assert first.held("mail:a")
    await second.acquire("mail:b")  # другие identity — свободно
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1.0):
            await second.acquire("mail:a")
    assert not second.held("mail:a")

    await first.release("mail:a")
    async with asyncio.timeout(1.0):
        await second.acquire("mail:a")
    await second.release("mail:a")
    await second.release("mail:b")


async def test_file_identity_lock_queues_own_waiters(tmp_path: Path) -> None:
    lock = FileIdentityLock(tmp_path)
    order: list[int] = []

    async def take(number: int) -> None:
        await lock.acquire("k")
        order.append(number)
        await asyncio.sleep(0.2)
        await lock.release("k")

    await asyncio.gather(*(take(number) for number in range(3)))
    assert sorted(order) == [0, 1, 2]
    assert not lock.held("k")


async def test_file_identity_lock_release_of_free_identity_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="не занята"):
        await FileIdentityLock(tmp_path).release("mail:a")


@pytest.mark.parametrize("key", ["mail:42", "a/b\\c", "..", "", "аккаунт", "x" * 500, 'con<>:"|?*'])
def test_safe_file_name(key: str) -> None:
    name = safe_file_name(key)
    assert name
    assert len(name) <= 80
    assert all(char.isascii() and (char.isalnum() or char in "._-") for char in name)
    assert not name.startswith(".")


def test_safe_file_names_differ_for_different_keys() -> None:
    assert safe_file_name("mail:1") != safe_file_name("mail_1")
