# Примеры

Команды — из корня репозитория. Примеры на браузере требуют экстру драйвера и сам браузер:
`uv sync --extra playwright && uv run playwright install chromium` или `uv sync --extra pydoll` (нужен Chrome).

| Пример | Что показывает | Браузер | Запуск |
|---|---|---|---|
| [`start/`](start) | три шага страницы «Начало работы»: одна вкладка, три identity сразу, вход через flow; вывод сверяется с тем, что показывает документация | Playwright, живой сайт quotes.toscrape.com | `uv run --extra playwright python -m examples.start.minimal`, `... readers`, `... login` |
| [`quickstart.py`](quickstart.py) | весь API на одной странице: пул, identity, flow, аренда, `any_of`, `pool.map` | нет, `FakeDriver` | `uv run python -m examples.quickstart` |
| [`quotes/`](quotes) | SDK сайта отдельным слоем; вход, сессии между запусками пула, несколько вкладок на аккаунт, окна для отладки; кто хранит сессию — пул или SDK | Playwright, живой сайт quotes.toscrape.com | `uv run --extra playwright python -m examples.quotes.app` |
| [`accounts/`](accounts) | один SDK почты на двух SDK браузера; аккаунты за прокси (sticky), падение браузера посреди работы — повтор без повторного входа | Playwright или pydoll, локальный сайт | `uv run --extra playwright python -m examples.accounts.app` |
| [`demos/`](demos) | сценарии «до» и «после» для роликов документации: шесть окон и пять задач одного аккаунта, без пула и с пулом | Playwright, сеть не нужна | `uv run --extra playwright python -m examples.demos.windows --mode after`, `... -m examples.demos.login --mode after` |

## Как устроены `quotes` и `accounts`

Так, как устроен настоящий проект, где сайт описан отдельной библиотекой:

```
quotes/
  quotes_sdk/     слой 1 — SDK сайта: адреса, селекторы, вход, ошибки сайта.
                  Про пул не знает: browser_pool не импортирует. Работает и без пула.
  app/            слой 2 — оркестрация: единственное место, где встречаются SDK и пул.
    flow.py         как открыть сессию identity — вызовами SDK
    errors.py       ошибки SDK -> виды сбоя пула (classify)
    pool.py         сборка пула: драйвер, конфиг, хранилище сессий, окна
    __main__.py     работа: pool.run / pool.map над операциями SDK
accounts/
  mail_sdk/       слой 1 — SDK почты; playwright.py и pydoll.py — один интерфейс (base.py)
  app/            слой 2 — flow, classify, выбор SDK браузера, сценарий
  harness/        стенд — не часть приложения: локальный сайт, прокси, «падение» браузера
```

Правило слоёв проверяет `tests/test_examples.py`: SDK, импортирующий `browser_pool`, и пример, берущий
внутренности пула (`browser_pool._core`) или приватные имена, роняют тест.

## Режимы

`quotes`:

- `--mode headless | windows | tabs | tabs-grouped` — без окон; окно на аккаунт; окно на вкладку; окна аккаунта рядом;
- `--sessions pool | sdk` — сессии хранит пул (`FileStateStore`) или SDK (`SessionVault`, у identity
  `StatePolicy(mode="none")`);
- `--accounts`, `--parallel` (вкладок на аккаунт одновременно), `--pages`, `--hold`.

`accounts`:

- `--driver playwright | pydoll`, `--accounts`, `--rounds`, `--headed`, `--json` (итог одной строкой для тестов).

Код выхода 0 — сценарий прошёл: всё прочитано, по одному входу паролем на аккаунт.
