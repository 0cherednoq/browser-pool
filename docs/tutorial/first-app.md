# Первое приложение

Почта на шести аккаунтах, которая переживает падение браузера.

В [начале работы](quickstart.md) {term}`пул` показан по частям. Здесь из тех же частей собрано приложение, которое
переживает падение браузера. Это пример [`examples/accounts`](../../examples/accounts) из репозитория; весь код ниже
подключён из него, и его гоняют тесты.

Что делает приложение: шесть аккаунтов почты за тремя прокси, каждый аккаунт закреплён за одним из них, четыре
раунда проверки ящика. Во втором раунде процесс одного браузера убивается посреди работы. В конце должно выйти так: все проверки сделаны, браузер
перезапущен, по паролю каждый аккаунт вошёл ровно один раз.

## Запуск

Сайт почты и прокси поднимаются локально, внешняя сеть не нужна.

```bash
git clone https://github.com/0cherednoq/browser-pool
cd browser-pool
uv sync --extra playwright
uv run playwright install chromium
uv run python -m examples.accounts.app
```

Прогон занимает несколько секунд и печатает итог.

:::{admonition} Результат запуска
:class: tip run-result

```text
2026-10-02 22:08:41,050 WARNING browser_pool._core.supervisor: Браузер browser-0 в карантине: соединение с браузером потеряно
          accounts: 6
            rounds: 4
        tasks_done: 24
      task_retries: 1
        killed_pid: 21444
  browser_restarts: 1
   password_logins: {'user4': 1, 'user0': 1, 'user3': 1, 'user2': 1, 'user1': 1, 'user5': 1}
    proxy_requests: {'proxy-0': 22, 'proxy-1': 14, 'proxy-2': 39}
            passed: True
```
:::

Двадцать четыре задачи выполнены, одна из них повторялась, браузер перезапускался один раз, и у каждого аккаунта
один вход по паролю. Номер процесса и число запросов через прокси от запуска к запуску меняются.

:::{tip}
С флагом `--headed` браузеры видны. То же приложение идёт и на pydoll: поставьте экстру (`uv sync --extra pydoll`,
нужен установленный Chrome) и добавьте `--driver pydoll`.
:::

## SDK сайта

Код, который знает сайт, лежит отдельно и о пуле не знает: `browser_pool` он не импортирует. Интерфейс у него
обычный, три операции на одной вкладке:

```{literalinclude} ../../examples/accounts/mail_sdk/base.py
:caption: mail_sdk/base.py
:language: python
:pyobject: MailClient
```

Реализация на Playwright получает нативную `Page` и работает с ней напрямую:

```{literalinclude} ../../examples/accounts/mail_sdk/playwright.py
:caption: mail_sdk/playwright.py
:language: python
:pyobject: PlaywrightMailClient
```

Такой SDK можно вызвать и без пула, на вкладке, которую вы открыли сами, поэтому его легко отлаживать отдельно.

## Flow

{term}`Flow <flow>` отвечает на вопрос пула «как сделать {term}`контекст` этого аккаунта рабочим». Он тонкий:
достаёт аккаунт из `payload` {term}`identity` и вызывает SDK.

```{literalinclude} ../../examples/accounts/app/flow.py
:caption: app/flow.py
:language: python
:pyobject: MailFlow
```

`open` пул вызывает один раз на контекст. Перед вызовом он уже восстановил сохранённую {term}`сессию <сессия>`,
поэтому после перезапуска браузера проверка входа в SDK отвечает «да», и до ввода пароля дело не доходит. Отсюда и
берётся «один вход по паролю на аккаунт» в итоге прогона.

Аккаунт лежит в `payload`, пароль в `repr` не попадает:

```{literalinclude} ../../examples/accounts/app/flow.py
:caption: app/flow.py
:language: python
:pyobject: Account
```

## Ошибки сайта и виды сбоев

SDK бросает свои ошибки. Таблица, которая переводит их в виды сбоев пула, живёт на слое приложения:

```{literalinclude} ../../examples/accounts/app/errors.py
:caption: app/errors.py
:language: python
:pyobject: classify
```

Неверный пароль блокирует identity, и повторов не будет. Потерянная сессия выводит контекст из работы, и следующая
{term}`аренда` войдёт заново. Упавший браузер и недоступный прокси здесь не упомянуты: их распознаёт
{term}`драйвер`.

## Сборка пула

```{literalinclude} ../../examples/accounts/app/scenario.py
:caption: app/scenario.py
:language: python
:pyobject: make_app
```

Что здесь задано:

- два браузера, в каждом до трёх вкладок и четырёх контекстов;
- хранилище сессий в каталоге, по файлу на аккаунт;
- список из трёх прокси;
- flow и классификатор из предыдущих разделов.

:::{note}
`spawn_delay=0.0` и короткая пауза перезапуска нужны только для того, чтобы пример шёл быстро.
:::

Закрепление за прокси задаёт сама identity, политикой `ProxyPolicy.sticky()`:

```{literalinclude} ../../examples/accounts/app/scenario.py
:caption: app/scenario.py
:language: python
:start-at: identities = [
:end-at: "]"
:dedent: 8
```

Аккаунт получает один и тот же прокси в каждом раунде и после перезапуска браузера.

## Работа

Раунд отдаёт пулу задачу для каждого аккаунта и просит повторять её при сбоях окружения:

```{literalinclude} ../../examples/accounts/app/scenario.py
:caption: app/scenario.py
:language: python
:pyobject: _round
```

Задача `check` получает аренду и работает с `lease.page` через тот же SDK почты. Ни замков, ни проверки «вошёл ли
аккаунт», ни обработки упавшего браузера в ней нет.

Строка с `crash` относится к стенду: во втором раунде она убивает процесс браузера прямо во время аренды. После
этого:

1. Пул замечает обрыв соединения с браузером и отправляет его в карантин. В логе появляется строка из вывода выше.
2. Задача падает с ошибкой SDK. Драйвер распознаёт в ней умерший браузер, вид сбоя `browser`, а этот вид
   повторяется.
3. `pool.map` повторяет упавшую задачу в новой аренде, а пул тем временем перезапускает браузер.
4. Контексты аккаунтов открываются заново с сохранёнными сессиями, `open` видит живой вход и пароль не вводит.

## Что дальше

- Кто хранит сессию, разобрано на странице [Как написать SessionFlow](../extending/flow.md).
- Чтобы увидеть окна аккаунтов сеткой, добавьте секцию `windows`: [Отладка](../guide/debug.md).
- Второй пример, [`examples/quotes`](../../examples/quotes), работает с живым сайтом и показывает несколько вкладок
  на аккаунт.

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`sign-in` Вход
:link: ../guide/login
:link-type: doc

Один вход на аккаунт, без гонок между задачами.
:::

:::{grid-item-card} {octicon}`sync` Сбои и повторы
:link: ../guide/recovery
:link-type: doc

Вид сбоя, повторы и `lease.commit()`.
:::
::::
