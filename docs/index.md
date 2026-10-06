---
layout: landing
---

# browser-pool

Пул браузеров для асинхронного Python. Он держит контексты аккаунтов, сессии, прокси и тёплые вкладки и
восстанавливает их после сбоев.

:::{container} buttons

[Начало работы](tutorial/quickstart.md)
[GitHub](https://github.com/0cherednoq/browser-pool)
:::

Код, который знает сайт, получает нативные объекты Playwright, pydoll или своего браузерного SDK, без обёрток.
Пул отвечает за остальное: в каком браузере и контексте открыта вкладка, с каким прокси и сессией, и что делать,
когда браузер упал, прокси умер или сайт выкинул из аккаунта.

```{literalinclude} ../examples/start/minimal.py
:caption: minimal.py
:language: python
```

:::{admonition} Результат запуска
:class: tip run-result

```{literalinclude} tutorial/output/minimal.txt
:language: text
```
:::

:::{warning}
Библиотека в стадии альфа: API может меняться между минорными версиями. Изменения описаны в
[журнале изменений](reference/changelog.md).
:::

## Что берёт на себя пул

::::{grid} 1 2 3 3
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`sign-in` Один вход на аккаунт
:link: guide/login
:link-type: doc

На пять задач одного аккаунта приходится один вход. Сессия переживает падение браузера, а с хранилищем на диске и
перезапуск процесса.
:::

:::{grid-item-card} {octicon}`sync` Сбои и повторы
:link: guide/recovery
:link-type: doc

Вы говорите, что сломалось, пул чинит ровно это и повторяет задачу. После `lease.commit()` необратимое действие
не повторится.
:::

:::{grid-item-card} {octicon}`globe` Прокси
:link: guide/proxies
:link-type: doc

Список в привычных форматах, постоянный прокси у аккаунта, пауза для мёртвых, часовой пояс по стране выхода.
:::

:::{grid-item-card} {octicon}`stack` Параллельность
:link: guide/parallelism
:link-type: doc

Потолки на браузеры, вкладки и аккаунты задаются конфигом. Остальные задачи ждут в очереди с приоритетами.
:::

:::{grid-item-card} {octicon}`browser` Отладка окон
:link: guide/debug
:link-type: doc

Окна аккаунтов разложены сеткой и подписаны. Упавшая вкладка по запросу остаётся открытой.
:::

:::{grid-item-card} {octicon}`beaker` Тесты без браузера
:link: guide/testing
:link-type: doc

Фейковый драйвер и виртуальное время: паузы и повторы проверяются за миллисекунды.
:::
::::

Ещё пул следит за [памятью и процессами хоста](guide/resources.md) и показывает своё состояние
[снимком, событиями и метриками](guide/observability.md).

## Куда дальше

::::{grid} 1 2 4 4
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`play` Начало работы
:link: tutorial/quickstart
:link-type: doc

От установки до трёх аккаунтов.
:::

:::{grid-item-card} {octicon}`book` Основные понятия
:link: explanation/concepts
:link-type: doc

Identity, контекст, сессия, аренда.
:::

:::{grid-item-card} {octicon}`rocket` Первое приложение
:link: tutorial/first-app
:link-type: doc

Шесть аккаунтов и упавший браузер.
:::

:::{grid-item-card} {octicon}`code` Справочник
:link: reference/api/index
:link-type: doc

Классы, конфиг, события, ошибки.
:::
::::

```{toctree}
:caption: Начало
:hidden:
:maxdepth: 1

tutorial/quickstart
explanation/concepts
tutorial/first-app
```

```{toctree}
:caption: Задачи
:hidden:
:maxdepth: 1

guide/login
guide/parallelism
guide/proxies
guide/recovery
guide/resources
guide/debug
guide/observability
guide/testing
```

```{toctree}
:caption: Свой код
:hidden:
:maxdepth: 1

extending/flow
extending/driver
extending/provider
```

```{toctree}
:caption: Устройство
:hidden:
:maxdepth: 1

explanation/architecture
explanation/errors
explanation/decisions
```

```{toctree}
:caption: Справочник
:hidden:
:maxdepth: 1

reference/config
reference/events
reference/errors
reference/api/index
reference/changelog
```
