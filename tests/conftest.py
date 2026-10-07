"""Тесты библиотеки гоняются на её же тестовом наборе: виртуальное время и фейковый драйвер.

Ручные чек-листы (`@manual`: headed-окна, на которые нужно смотреть) пропускаются, пока не
передан `--manual`.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester", "browser_pool.testing.pytest_plugin"]


@pytest.fixture(autouse=True)
def _screen_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты окон не зависят от экрана машины: на Linux без `DISPLAY` (CI) пул выключил бы секцию окон.

    Настоящие браузеры в тестах окон идут headless через аргумент, экран им не нужен.
    """
    monkeypatch.setattr("browser_pool.windows.manager.display_available", lambda: True)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--manual",
        action="store_true",
        help="запустить ручные чек-листы (@manual): headed-окна, на которые нужно смотреть",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Тесты библиотеки гоняются на виртуальном времени (плагин включает его только по метке).

    Метка нужна до того, как pytest-asyncio выберет цикл событий (он делает это здесь же, при
    параметризации). Настоящему браузеру и настоящему вводу-выводу — метка `browser`, обычный цикл.
    """
    if metafunc.definition.get_closest_marker("browser") is None:
        metafunc.definition.add_marker(pytest.mark.virtual_time)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--manual"):
        return
    skip = pytest.mark.skip(reason="ручной чек-лист: pytest --manual")
    for item in items:
        if item.get_closest_marker("manual") is not None:
            item.add_marker(skip)
