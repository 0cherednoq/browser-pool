# Разработка browser-pool

```bash
uv sync --all-extras             # инструменты из группы dev и все драйверы
uv run playwright install chromium
uv run pre-commit install        # быстрые проверки перед каждым коммитом
uv run poe check                 # полный набор — перед завершением любого шага
uv run poe matrix                # то, что зависит от интерпретатора, — на Python 3.12, 3.13, 3.14
uv run poe fix                   # автоисправление и форматирование
uv run python scripts/soak.py --driver playwright --minutes 30   # длинный прогон на настоящем браузере
```

Выпуск запускается тегом `v<версия>` (`.github/workflows/release.yml`): полный набор проверок, сборка, `twine check`, колесо в
чистом окружении на Linux и Windows под 3.12 и 3.14, затем TestPyPI и PyPI (trusted publishing). То же колесо локально:

```bash
uv build && uv run python scripts/smoke_wheel.py dist/browser_pool-*.whl --python 3.12
```

Что входит в `poe check` и зачем:

| Задача | Инструмент | Что держит |
|---|---|---|
| `lint` | ruff format и ruff check | стиль; корректность; API библиотеки: keyword-only параметры, без boolean trap, докстринги у публичного; SDK браузеров только в адаптерах и только лениво; время только через часы пула |
| `complexity` | complexipy | когнитивная сложность функции не выше 15 |
| `types` | basedpyright strict | типы внутри пакета, тестов, скриптов и примеров; мёртвые ветки, циклы импортов, `@override` |
| `api` | basedpyright `--verifytypes` | публичный API полностью типизирован глазами потребителя (`py.typed`) |
| `arch` | import-linter | слои пакета; драйверы и провайдеры не знают друг о друге; новый модуль обязан попасть в схему слоёв |
| `stdlib` | `scripts/check_stdlib_only.py` | ядро импортирует только stdlib, у пакета нет обязательных зависимостей |
| `slop` | ast-grep (`rules/`) | секреты не попадают в `repr`; библиотека не настраивает логирование и не владеет циклом событий; без заглушек |
| `docs` | `scripts/config_reference.py --check`, `scripts/reference_tables.py --check` | справочники конфига, событий и ошибок в `docs/reference/` совпадают с кодом |
| `test` | pytest | тесты; любое предупреждение считается ошибкой; тесты с настоящим браузером помечены `browser` |

Поддерживается Python 3.12 и новее. ruff и basedpyright целятся в 3.12, так что синтаксис и API новее ловятся в `check` на
любой версии. `poe matrix` гоняет на 3.12 и 3.13 то, что от версии зависит (`test`, `stdlib`, `docs`), в окружениях
`.venv-3.X`, их создаёт uv.

Сайт документации собирает Sphinx из каталога `docs/` (настройка в `docs/conf.py`). В `poe check` сборка не входит, для неё нужна группа зависимостей `docs`:

```bash
uv sync --all-extras --group docs   # без --group docs следующий sync уберёт Sphinx из окружения
uv run poe site                     # сайт в docs/_build/html; любое предупреждение Sphinx — ошибка
uv run poe prose                    # проза страниц: длинное тире, знаки в прозе и другие жёсткие запреты
```

После правки страницы запускаются `poe prose`, `poe docs`, `pytest tests/test_docs_names.py tests/test_docs_api.py`
и `poe site`. Справочники `docs/reference/config.md`, `events.md` и `errors.md` руками не правятся: их пишут
`scripts/config_reference.py` и `scripts/reference_tables.py`. Ролики для страниц записывает
`scripts/record_demo.py` (Windows, нужен ffmpeg); в кадр попадает весь рабочий стол.

Архитектура в `[tool.importlinter]` описана заранее целиком: слой в скобках опционален, его модуля может ещё не быть.
Новый модуль верхнего уровня отнесите к слою в `pyproject.toml`, иначе `poe arch` упадёт.

Ручные чек-листы окон (видимые окна на экране секунд на 10):
`uv run pytest tests/test_playwright_windows.py tests/test_pydoll_windows.py --manual`.
