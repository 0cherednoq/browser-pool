"""Плагин pytest: фейковый драйвер со сторожем утечек и виртуальное время — по метке.

Подключение в `conftest.py` (нужен `pip install browser-pool[testing]`: pytest и pytest-asyncio ≥ 1.4)::

    pytest_plugins = ["browser_pool.testing.pytest_plugin"]

Что даёт:

- фикстура `fake_driver` — `FakeDriver`, который после теста проверяет, что ничего не
  утекло: ни браузеров, ни контекстов, ни вкладок, ни задач, начатых тестом (чужие задачи — фикстур с
  более широким `loop_scope` — сторож не трогает);
- метка `virtual_time` — тест идёт на `VirtualTimeLoop`: TTL и таймауты пула проверяются без
  ожидания. Без метки async-тест идёт на обычном цикле событий и обычном времени: плагин не меняет
  поведение чужих тестов. Под меткой время не прыгает, пока в цикле есть настоящий ввод-вывод
  (сокеты, потоки): `asyncio.timeout` вокруг сетевого чтения не срабатывает раньше ответа;
- метки `virtual_time` и `browser` зарегистрированы — `--strict-markers` их принимает.

Режим pytest-asyncio — какой у проекта: в `strict` async-тестам нужна метка `asyncio`
(`pytestmark = pytest.mark.asyncio`), в `auto` — нет. Плагин не требует `asyncio_mode = "auto"`.
"""

from __future__ import annotations

import asyncio
from importlib import metadata
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from browser_pool.testing.fake_driver import FakeDriver
from browser_pool.testing.virtual_time import VirtualTimeLoop

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

MIN_PYTEST_ASYNCIO = (1, 4)
"""Первая версия pytest-asyncio с хуком `pytest_asyncio_loop_factories`."""


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.split("."):
        digits = "".join(character for character in piece if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_pytest_asyncio(installed: str) -> None:
    """Версия pytest-asyncio годится плагину, иначе `ImportError` с понятным текстом."""
    if _version_tuple(installed) < MIN_PYTEST_ASYNCIO:
        needed = ".".join(map(str, MIN_PYTEST_ASYNCIO))
        msg = (
            f"browser_pool.testing.pytest_plugin требует pytest-asyncio ≥ {needed} "
            f"(установлен {installed}): pip install -U 'browser-pool[testing]'"
        )
        raise ImportError(msg)


check_pytest_asyncio(metadata.version("pytest-asyncio"))


def pytest_configure(config: pytest.Config) -> None:
    """Зарегистрировать метки плагина: с `--strict-markers` они иначе — ошибка сбора."""
    config.addinivalue_line(
        "markers",
        "virtual_time: async-тест идёт на цикле событий с виртуальным временем (TTL и таймауты без ожидания)",
    )
    config.addinivalue_line(
        "markers", "browser: нужен настоящий браузер; идёт на обычном цикле событий"
    )


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Виртуальное время — только тестам с меткой `virtual_time` (и без `browser`)."""
    _ = config
    if (
        item.get_closest_marker("virtual_time") is not None
        and item.get_closest_marker("browser") is None
    ):
        return {"virtual": VirtualTimeLoop}
    return {"default": asyncio.new_event_loop}


@pytest_asyncio.fixture
async def fake_driver() -> AsyncIterator[FakeDriver]:
    """Фейковый драйвер; после теста — проверка, что ничего не утекло."""
    driver = FakeDriver()
    before = set(asyncio.all_tasks())
    yield driver
    # Дать отработать тому, что закрывается колбэками (события disconnected и т.п.).
    await asyncio.sleep(0)
    leaked = driver.live
    current = asyncio.current_task()
    tasks = [
        task
        for task in asyncio.all_tasks()
        if task is not current and task not in before and not task.done()
    ]
    for task in tasks:
        task.cancel()
    problems: list[str] = []
    if any(leaked):
        problems.append(f"живые ресурсы драйвера (браузеры, контексты, вкладки): {tuple(leaked)}")
    if tasks:
        problems.append(f"незавершённые задачи: {[task.get_name() for task in tasks]}")
    if problems:
        pytest.fail("Утечка после теста: " + "; ".join(problems), pytrace=False)
