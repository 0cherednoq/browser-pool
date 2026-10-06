# browser-pool

Пул браузеров для асинхронного Python. Он держит контексты аккаунтов, сессии, прокси и тёплые вкладки и восстанавливает
их после сбоев. Работает поверх Playwright, pydoll или своего браузерного SDK.

Код, который знает сайт (вход, селекторы, операции), пишется отдельно и получает нативные объекты своего SDK без
обёрток: `Page` у Playwright, `Tab` у pydoll. `browser-pool` отвечает за остальное: в каком браузере и контексте открыта
страница, с каким прокси и сессией, что делать, когда браузер упал, прокси умер или сайт выкинул из аккаунта.

> **Альфа.** API может меняться между минорными версиями, каждое изменение описано в [CHANGELOG](CHANGELOG.md).
> Ставится с `--pre`.

## Какие проблемы снимает

### Окна друг на друге

Запустили шесть аккаунтов, и шесть окон открылись в одной точке экрана: видно только последнее.
`Windows(mode="per_context")` раскладывает окна сеткой без перекрытий. У аккаунта одно окно, его задачи идут вкладками
в этом окне, а с `Debug(label_windows=True)` в заголовке написано, чьё оно. `Debug(hold_on_error=60)` оставляет
упавшую вкладку открытой, чтобы посмотреть, что было на странице. Подробнее: [Отладка](docs/guide/debug.md).

### Самодельные семафоры

Отдайте пулу все задачи сразу. Браузеров, вкладок в браузере и вкладок у одного аккаунта будет не больше, чем
разрешено в конфиге, остальные задачи ждут в очереди с приоритетами. С `any_of` задача получает любой свободный
аккаунт, в первую очередь уже залогиненный. Подробнее: [Параллельность](docs/guide/parallelism.md).

### Гонки при входе

На пять задач одного аккаунта приходится один вход: остальные ждут его и получают залогиненный контекст. Одновременно
идёт не больше `concurrent_opens` входов, а через один прокси входы идут по одному. Сессия сохраняется и переживает
падение браузера, а с `FileStateStore` и перезапуск процесса. С `FileIdentityLock` аккаунт не откроют дважды и
разные процессы. Подробнее: [Вход](docs/guide/login.md).

### Зависшие Chrome и своп

Когда не хватает памяти или CPU, пул перестаёт расти и закрывает простаивающее. Распухшие браузеры он
перезапускает, дав их задачам доработать.
После остановки не остаётся ни одного процесса Chrome, а браузеры упавшего процесса добивает следующий запуск.
Подробнее: [Ресурсы хоста](docs/guide/resources.md).

### Прокси

Список принимается в привычных форматах: `host:port`, `user:pass@host:port` и других. Аккаунт закреплён за своим прокси и после перезапуска, мёртвые прокси уходят на
паузу сами. Часовой пояс и локаль подставляются по стране выхода. Аккаунты держатся на дешёвых прокси, пока сайт их
пускает. Подробнее: [Прокси](docs/guide/proxies.md).

### Обработка ошибок

Вы говорите, *что* сломалось: вкладка, сессия, прокси, аккаунт или браузер. Пул чинит ровно это и повторяет задачу на
исправных ресурсах. После `lease.commit()` необратимое действие не повторится. Подробнее:
[Сбои и повторы](docs/guide/recovery.md).

### Не видно, что делает пул

Пул отдаёт снимок своего состояния без секретов, типизированные события и метрики Prometheus, а с упавшей вкладки
снимает скриншот и HTML. Подробнее: [Наблюдение](docs/guide/observability.md).

### Логику не проверить без браузера

`FakeDriver` заменяет браузер и выдаёт сбои по заказу теста. С виртуальным временем паузы и повторы проверяются за
миллисекунды. Подробнее: [Тесты](docs/guide/testing.md).

## Установка

```bash
pip install --pre "browser-pool[playwright]"   # Python 3.12+
playwright install chromium
```

Или `browser-pool[pydoll]`, для него нужен установленный Chrome. У ядра зависимостей нет. Экстры: `playwright`,
`pydoll`, `prometheus` (метрики), `resources` (защита хоста по памяти и CPU, psutil), `testing` (плагин pytest).

## Пример

```python
from pathlib import Path

from browser_pool import BaseFlow, BrowserPool, Identity, PoolConfig, ProxyPolicy
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.proxies import ProxyList
from browser_pool.state import FileStateStore


class MailFlow(BaseFlow):
    """Как открыть сессию аккаунта: пул зовёт это один раз на контекст."""

    async def open(self, ctx):
        account = ctx.identity.payload
        client = mail_sdk.Client(await ctx.new_page())  # ваш SDK сайта
        if not await client.is_logged_in():  # сессию пул уже восстановил
            await client.login(account.login, account.password)
        return account.login


accounts = [
    Identity(key=f"mail:{a.login}", payload=a, proxy=ProxyPolicy.sticky()) for a in load_accounts()
]
proxies = ProxyList.parse_lines(
    Path("proxies.txt").read_text().splitlines()
)  # закрепление — у identity

async with BrowserPool(
    PlaywrightDriver(),
    config=PoolConfig.accounts(),
    flow=MailFlow(),
    classifier=mail_errors_to_pool,  # ошибки SDK сайта -> что сломалось
    state_store=FileStateStore("sessions"),  # вход переживает перезапуск процесса
    proxy_source=proxies,
) as pool:

    async def send(lease):
        lease.commit()  # дальше письмо могло уйти — эту попытку больше не повторять
        await mail_sdk.Client(lease.page).send(message)

    # Любой свободный аккаунт; упало до commit из-за вкладки, сессии, прокси или браузера — ещё раз.
    await pool.run(send, any_of=accounts, retries=2)
```

Рабочие примеры лежат в [`examples/`](examples/README.md): SDK сайта отдельным слоем, который о пуле не знает, и
приложение, которое соединяет его с пулом.

## Драйверы

| Драйвер | Экстра | Изоляция | Состояние сессии | Окна для отладки |
|---|---|---|---|---|
| `browser_pool.drivers.playwright.PlaywrightDriver` | `playwright` | контекст на аккаунт, прокси на контексте | куки и localStorage | Chromium: на лету, окно на вкладку |
| `browser_pool.drivers.pydoll.PydollDriver` | `pydoll` (pydoll 2.27 и новее, проверены 2.27 и 3.0) | контекст на аккаунт, прокси на контексте | куки; профиль на диске | на лету, окно на аккаунт |

Уже запущенные браузеры подключаются через `RemoteCDP`, профили антидетекта через `AdsPowerProvider`
(`browser_pool.providers`). `PlaywrightDriver` проверен на Chromium. Firefox и WebKit в нём не проверялись: у них нет
CDP, поэтому окна, перехват авторизации прокси и `pid` работают иначе или не работают.

Свой SDK подключается классом на основе `browser_pool.driver.BaseDriver`. Обязательны запуск, контекст, вкладка и их
закрытие, остальное по умолчанию отвечает «не умею». Класс объявляет свои возможности, и по ним пул сам выбирает
изоляцию (прокси на контексте, на браузере или у вендора), а синхронному SDK даёт по потоку на браузер.

## Публичный API

Поддерживается то, что экспортирует `browser_pool`, и подпакеты `browser_pool.drivers`, `.providers`, `.proxies`,
`.state`, `.events`, `.testing`, а также контрактные модули `driver`, `provider`, `flow`, `hooks`, `config`, `errors`,
`snapshot`, `geometry`. Всё, что начинается с подчёркивания (`browser_pool._core`), внутреннее и меняется без
предупреждения. Границу проверяет `tests/test_public_api.py`.

## Документация

Сайт собирается из каталога `docs/` (`uv run poe site`); те же страницы читаются и здесь, в репозитории.

- Начало: [начало работы](docs/tutorial/quickstart.md) (три запускаемых шага на настоящем браузере),
  [основные понятия](docs/explanation/concepts.md), [первое приложение](docs/tutorial/first-app.md) на шести
  аккаунтах.
- [Задачи](docs/guide/README.md): вход, параллельность, прокси, сбои и повторы, ресурсы хоста, отладка окон,
  наблюдение, тесты.
- Свой код: [SessionFlow](docs/extending/flow.md), [свой драйвер](docs/extending/driver.md),
  [провайдер браузеров](docs/extending/provider.md).
- Устройство: [устройство пула](docs/explanation/architecture.md), [виды сбоев](docs/explanation/errors.md),
  [границы библиотеки](docs/explanation/decisions.md).
- Справочник: [конфиг](docs/reference/config.md), [события](docs/reference/events.md),
  [ошибки](docs/reference/errors.md); справочник API по модулям есть на сайте.
- [Примеры](examples/README.md), [CHANGELOG](CHANGELOG.md), [разработка](CONTRIBUTING.md).

## Лицензия

MIT.
