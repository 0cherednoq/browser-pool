"""Плагин pytest (M8.19): виртуальное время — по метке, метки зарегистрированы, примеры гайда живут в strict-режиме."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from browser_pool.testing.pytest_plugin import check_pytest_asyncio

PLUGIN = "browser_pool.testing.pytest_plugin"
CONFTEST = f'pytest_plugins = ["{PLUGIN}"]\n'
GUIDE = Path(__file__).resolve().parents[1] / "docs" / "guide" / "testing.md"


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    # Свой ini-файл в каталоге проекта: иначе корнем станет каталог выше с чужим pyproject.toml
    # (например, в профиле пользователя), и pytest пойдёт перебирать соседние временные каталоги.
    pytester.makeini("[pytest]")
    # Вывод дочернего процесса — в UTF-8: на Windows в канале он иначе в cp1251, а в ответах плагина кириллица.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PYTHONUTF8", "1")
        patch.setenv("PYTHONIOENCODING", "utf-8")
        return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-W", "error", *args)


def outcomes(result: pytest.RunResult) -> dict[str, int]:
    return result.parseoutcomes()


# --- время по метке --------------------------------------------------------------------------


def test_unmarked_async_tests_run_on_the_ordinary_loop(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(
        """
        import asyncio
        import pytest

        pytestmark = pytest.mark.asyncio


        async def test_plain():
            assert type(asyncio.get_running_loop()).__name__ != "VirtualTimeLoop"


        @pytest.mark.virtual_time
        async def test_marked():
            loop = asyncio.get_running_loop()
            assert type(loop).__name__ == "VirtualTimeLoop"
            started = loop.time()
            await asyncio.sleep(3600)
            assert loop.time() - started >= 3600


        @pytest.mark.virtual_time
        @pytest.mark.browser
        async def test_browser_wins():
            assert type(asyncio.get_running_loop()).__name__ != "VirtualTimeLoop"
        """
    )

    result = run(pytester, "--strict-markers")

    assert outcomes(result) == {"passed": 3}, result.stdout.str()


def test_virtual_time_does_not_fire_before_real_network_answers(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(
        """
        import asyncio
        import socket
        import threading
        import time

        import pytest

        pytestmark = [pytest.mark.asyncio, pytest.mark.virtual_time]


        def slow_server(listener):
            connection, _ = listener.accept()
            time.sleep(0.4)  # настоящая задержка собеседника
            connection.sendall(b"pong")
            connection.close()


        async def test_timeout_around_a_real_socket_does_not_fire_early():
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            threading.Thread(target=slow_server, args=(listener,), daemon=True).start()
            reader, writer = await asyncio.open_connection("127.0.0.1", listener.getsockname()[1])
            async with asyncio.timeout(30):
                assert await reader.read(4) == b"pong"
            writer.close()
            await writer.wait_closed()
            listener.close()
        """
    )

    result = run(pytester)

    assert outcomes(result) == {"passed": 1}, result.stdout.str()


# --- метки и версия ---------------------------------------------------------------------------


def test_plugin_registers_its_markers(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(
        """
        import pytest


        @pytest.mark.browser
        def test_a():
            pass


        @pytest.mark.virtual_time
        def test_b():
            pass
        """
    )

    result = run(pytester, "--strict-markers")

    assert outcomes(result) == {"passed": 2}, result.stdout.str()


@pytest.mark.parametrize("version", ["1.0.0", "1.3.2", "0.23"])
def test_old_pytest_asyncio_is_named_in_the_error(version: str) -> None:
    with pytest.raises(ImportError, match=r"pytest-asyncio ≥ 1\.4"):
        check_pytest_asyncio(version)


@pytest.mark.parametrize("version", ["1.4.0", "1.4", "2.0.1", "1.10.0rc1"])
def test_new_enough_pytest_asyncio_passes(version: str) -> None:
    check_pytest_asyncio(version)


# --- сторож утечек ----------------------------------------------------------------------------


def test_leak_guard_leaves_tasks_of_earlier_fixtures_alone(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(
        """
        import asyncio

        import pytest
        import pytest_asyncio

        pytestmark = pytest.mark.asyncio


        @pytest_asyncio.fixture
        async def background():
            task = asyncio.create_task(asyncio.sleep(3600))
            yield task
            assert task.cancelling() == 0, "сторож утечек отменил чужую задачу"
            task.cancel()


        async def test_guard(background, fake_driver):
            async with asyncio.timeout(1):
                await asyncio.sleep(0)


        async def test_leaked_task_is_still_reported(fake_driver):
            asyncio.create_task(asyncio.sleep(3600), name="забытая")
        """
    )

    result = run(pytester)

    out = result.stdout.str()
    # Оба теста прошли; утечку сторож сообщает ошибкой при завершении второго.
    assert outcomes(result) == {"passed": 2, "errors": 1}, out
    assert "забытая" in out
    assert "чужую задачу" not in out


# --- примеры гайда ----------------------------------------------------------------------------


def guide_examples() -> list[str]:
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split("## Фейковый драйвер и виртуальное время", 1)[1].split(chr(10) + "## ", 1)[
        0
    ]
    blocks = re.findall(r"```python\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 2, "в гайде ждём два примера: conftest.py и тест"
    return blocks


HARNESS = """from browser_pool import BaseFlow


class MailFlow(BaseFlow):
    async def open(self, ctx):
        return None


"""


def test_guide_example_runs_with_default_pytest_asyncio_settings(
    pytester: pytest.Pytester,
) -> None:
    conftest, example = guide_examples()
    pytester.makeconftest(conftest)
    pytester.makepyfile(test_mail=HARNESS + example)

    result = run(pytester, "--strict-markers")

    assert outcomes(result).get("passed") == 1, result.stdout.str()
