# Начало работы

От установки до трёх аккаунтов, которые входят на сайт по одному разу.

## Установка

Нужен Python 3.12 или новее.

::::{tab-set}
:sync-group: installer

:::{tab-item} uv
:sync: uv

```bash
uv add --prerelease allow "browser-pool[playwright]"
uv run playwright install chromium
```
:::

:::{tab-item} pip
:sync: pip

```bash
pip install --pre "browser-pool[playwright]"
playwright install chromium
```
:::
::::

Экстры выбирают браузерный SDK и дополнения:

- `browser-pool[playwright]` ставит драйвер Playwright;
- `browser-pool[pydoll]` ставит драйвер pydoll;
- `browser-pool[resources]` добавляет psutil для защиты хоста от нехватки памяти;
- `browser-pool[prometheus]` добавляет метрики;
- `browser-pool[testing]` добавляет плагин pytest.

Без экстр ставится ядро, у которого нет зависимостей.

:::{important}
Библиотека в стадии альфа. Без разрешения на предварительные версии (`--pre` у pip, `--prerelease allow` у uv)
установщик пакет не найдёт.
:::

## Первая вкладка

Пул создаётся с драйвером, а вкладка берётся в аренду на время блока `async with`.

```{literalinclude} ../../examples/start/minimal.py
:caption: minimal.py
:language: python
:linenos:
```

Сохраните файл и запустите его.

::::{tab-set}
:sync-group: installer

:::{tab-item} uv
:sync: uv

```bash
uv run python minimal.py
```
:::

:::{tab-item} pip
:sync: pip

```bash
python minimal.py
```
:::
::::

:::{admonition} Результат запуска
:class: tip run-result

```{literalinclude} output/minimal.txt
:language: text
```
:::

Что здесь произошло:

1. `BrowserPool(PlaywrightDriver())` создал {term}`пул`. {term}`Драйвер` сообщает пулу, каким SDK запускать
   браузеры. Сам браузер запустился позже, когда понадобилась первая вкладка.
2. `Identity(key="demo")` описала, от чьего имени работает браузер. У каждой {term}`identity` свой
   {term}`контекст` с отдельными куками.
3. `pool.page(identity)` выдал {term}`аренду <аренда>`. Другого способа получить вкладку нет, поэтому пул всегда
   знает, кто чем занят.
4. `lease.page` - обычная `Page` из Playwright, без обёртки. Всё, что умеет Playwright, доступно как есть.

На выходе из внутреннего `async with` вкладка вернулась в пул, на выходе из внешнего пул закрыл контексты и
браузер.

:::{note}
Примеры ходят на [quotes.toscrape.com](https://quotes.toscrape.com/). Это учебный сайт для автоматизации, его
форма входа принимает любой логин и пароль.
:::

## Несколько identity сразу

Теперь три identity читают три разные страницы одновременно. Данные приложения для identity лежат в `payload`. Пул
его не читает, и в `repr` identity он не попадает.

```{literalinclude} ../../examples/start/readers.py
:caption: readers.py
:language: python
:linenos:
:emphasize-lines: 19, 30
```

:::{admonition} Результат запуска
:class: tip run-result

```{literalinclude} output/readers.txt
:language: text
```
:::

`pool.map` взял аренду для каждой identity, выполнил в ней задачу и вернул результаты в порядке списка.

Контекстов получилось три, по одному на identity, а браузеров два. Пул разложил контексты по браузерам сам:
отдельный процесс на каждую identity не нужен, потому что куки изолирует контекст. Сколько браузеров и вкладок
держать, задаёт секция `Topology` [конфига](../reference/config.md).

## Вход на сайт

Аккаунту перед работой нужно войти. Если вход делает сама задача, пять задач одного аккаунта пойдут входить
одновременно. Поэтому вход описывается отдельно, в {term}`flow`, и пул вызывает его сам.

```{literalinclude} ../../examples/start/login.py
:caption: login.py
:language: python
:linenos:
:emphasize-lines: 33, 59
```

:::{admonition} Результат запуска
:class: tip run-result

```{literalinclude} output/login.txt
:language: text
```
:::

Задач было девять, а входов три.

1. `QuotesFlow.open` пул вызывает один раз на контекст аккаунта. Остальные задачи того же аккаунта ждут и
   получают уже открытую {term}`сессию <сессия>`.
2. `ctx.new_page()` даёт временную вкладку для входа. После `open` пул закроет её сам.
3. То, что вернул `open`, задача видит как `lease.session`.
4. Пароль лежит в `payload` и в `repr` identity не попадает.

:::{tip}
После успешного входа пул сохраняет куки и хранилище контекста. По умолчанию они лежат в памяти процесса. Чтобы
аккаунты не входили заново после перезапуска скрипта, передайте пулу `FileStateStore`, как показано на странице
[Вход](../guide/login.md).
:::

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`book` Основные понятия
:link: ../explanation/concepts
:link-type: doc

Identity, контекст, сессия, аренда и как они связаны.
:::

:::{grid-item-card} {octicon}`rocket` Первое приложение
:link: first-app
:link-type: doc

Шесть аккаунтов за прокси и браузер, который падает посреди работы.
:::
::::
