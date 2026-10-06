# Параллельность

Сколько браузеров, вкладок и аккаунтов работает одновременно.

200 задач на 10 аккаунтов нельзя просто отдать в asyncio.gather: откроется 200 вкладок, и машина встанет.
`Semaphore(8)` ограничивает число задач, но не следит за аккаунтами: шесть из восьми могут достаться одному, и сайт
увидит шесть параллельных сессий одного пользователя. Дальше понадобятся семафор на аккаунт, подсчёт вкладок в
браузере, очередь с приоритетами для срочных задач, выбор любого свободного аккаунта. В итоге получается свой
планировщик, который надо отлаживать и поддерживать.

В пуле этот планировщик уже есть, а ограничения задаются конфигом.

## Потолки в конфиге, очередь в пуле

Вы описываете ёмкость (сколько браузеров, сколько вкладок в браузере, сколько вкладок у одного аккаунта) и отдаёте
пулу все задачи сразу. Пул выдаёт вкладки, пока есть место. Остальные заявки ждут в очереди и получают вкладку, когда
она освобождается.

```python
import asyncio

from browser_pool import BrowserPool, Identity, PoolConfig, Topology
from browser_pool.drivers.playwright import PlaywrightDriver

accounts = [Identity(key=f"shop:{number}") for number in range(1, 4)]
urls = [f"https://quotes.toscrape.com/page/{number}/" for number in range(1, 11)] * 2

config = PoolConfig(
    topology=Topology(
        browsers=2,  # не больше двух процессов Chrome
        pages_per_browser=4,  # не больше четырёх вкладок в каждом
        pages_per_identity=2,  # у одного аккаунта не больше двух вкладок разом
    ),
)


async def main() -> None:
    async with BrowserPool(PlaywrightDriver(), config=config) as pool:

        async def scrape(url: str) -> str:
            async def task(lease) -> str:
                await lease.page.goto(url)
                return await lease.page.title()

            return await pool.run(task, any_of=accounts)  # любой свободный аккаунт

        titles = await asyncio.gather(*(scrape(url) for url in urls))  # все 20 задач сразу
        print(len(titles))


asyncio.run(main())
```

Все двадцать задач отдаются пулу сразу, а одновременно работают не больше шести. Два браузера по четыре вкладки
дают восемь мест, но три аккаунта по две вкладки занимают только шесть. Семафоров в коде нет.

## Какие бывают потолки

| Что ограничить | Где | По умолчанию |
|---|---|---|
| браузеров всего | `Topology.browsers` | 2 |
| браузеров, запущенных всегда (остальные - по спросу) | `Topology.min_browsers` | 0 |
| вкладок в браузере | `Topology.pages_per_browser` | 8 |
| аккаунтов (контекстов) в браузере | `Topology.contexts_per_browser` | 12 |
| вкладок у одного аккаунта | `Topology.pages_per_identity`, `Identity.max_pages` | без предела |
| вкладок и контекстов у группы аккаунтов | `Limits.groups` | без предела |
| длина очереди | `Limits.max_waiting` | без предела |
| браузеров, стартующих одновременно | `Limits.concurrent_launches` | 1 |
| входов на сайт одновременно | `Limits.concurrent_opens` | 4, см. [Вход](login.md) |

Новый аккаунт попадает в самый свободный здоровый браузер. Если в браузере уже `contexts_per_browser` аккаунтов, место
освобождает тот, кто дольше всех простаивает (его сессия перед этим сохраняется).

## Любой свободный аккаунт

```python
async with pool.page(any_of=accounts) as lease:
    print(lease.identity.key)  # кому досталось
```

`any_of` - список кандидатов в порядке предпочтения. Сначала выдаются аккаунты, у которых контекст уже открыт: вход
стоит дорого, и пул не станет логинить новый аккаунт, пока есть свободный залогиненный. Аккаунты на паузе и
заблокированные пропускаются.

## Очередь: сколько ждать и кто первый

```python
from browser_pool.errors import AcquireTimeoutError

try:
    async with pool.page(any_of=accounts, acquire_timeout=30, priority=10) as lease:
        ...
except AcquireTimeoutError:
    ...  # за 30 секунд свободной вкладки не нашлось
```

- `priority`: чем больше число, тем раньше заявка в очереди; при равном приоритете - по порядку прихода. Так срочная
  задача обгоняет пачку фоновых.
- Первая заявка в очереди не задерживает остальных: если её нельзя выдать сейчас (её аккаунт занят), пул выдаёт
  следующие, которые можно.
- `acquire_timeout` ограничивает только ожидание вкладки, а не работу задачи.
- `Limits(max_waiting=100)` не даёт очереди вырасти больше ста заявок: новая сразу получает `PoolSaturatedError`. Это
  удобно, когда задачи приходят из внешней очереди и лучше вернуть задачу туда, чем копить в памяти.
- Если заявка ждёт дольше минуты, снимок пула уходит в лог и в событие `AcquireWatchdog`. По нему видно, кто держит
  вкладки.

## Пачка задач

```python
results = await pool.map(task, accounts, concurrency=8, retries=2)
```

`pool.map` запускает задачу для каждого аккаунта, не больше `concurrency` одновременно, и повторяет её при сбоях
окружения. Результаты идут в том же порядке, что и аккаунты. С `return_exceptions=True` упавшая задача не отменяет остальные, а на её
месте в результатах стоит исключение.

## Один сайт не съедает весь пул

Когда пул общий для нескольких сайтов, тысяча задач одного из них может занять все вкладки. От этого защищает
групповой потолок по меткам identity:

```python
from browser_pool import GroupLimit, Identity, Limits, PoolConfig

mail_accounts = [Identity(key=f"mail:{n}", labels={"service": "mail"}) for n in range(50)]

PoolConfig(
    limits=Limits(
        groups=(GroupLimit(label="service", value="mail", max_pages=4, max_contexts=10),),
    ),
)
```

Почтовые аккаунты вместе занимают не больше четырёх вкладок и десяти контекстов, остальная ёмкость достаётся другим
сайтам.

## Тёплые вкладки: тяжёлая страница грузится один раз

Загружать на каждую задачу SPA, которая открывается десять секунд, слишком дорого. Поэтому вкладка в пуле живёт дольше
аренды: после задачи она возвращается тёплой, и следующая задача того же аккаунта получает её уже открытой.

```python
from browser_pool import BaseFlow


class InboxFlow(BaseFlow):
    async def open(self, ctx):
        ...  # вход

    async def prepare_page(self, session, page) -> None:
        await page.goto("https://mail.example/inbox")  # один раз на каждую новую вкладку

    async def reset_page(self, session, page) -> bool:
        return "/inbox" in page.url
```

Если вкладка ушла куда-то не туда, `reset_page` возвращает `False`, и пул её закрывает.

Сколько тёплых вкладок держать, задаёт `Topology.warm_pages_per_identity` (по умолчанию 1). Простаивающая тёплая
вкладка закрывается через `Lifecycle.page_idle_ttl`.

:::{note}
Если задача испортила вкладку, вызовите `lease.discard_page()`: вкладка закроется, а не вернётся в пул.
:::

## Контекст аккаунта целиком

Если ваш SDK сам открывает вкладки (например, всплывающие окна оплаты), берите контекст целиком:

```python
async with pool.context(account) as lease:
    page = await lease.context.new_page()  # вкладки, которые открыли, закрываете сами
```

:::{note}
Аренда контекста эксклюзивна: пока она держится, других аренд этого аккаунта нет.
:::

## Поменять ёмкость на ходу

```python
await pool.resize(browsers=4)
pool.reconfigure(limits=Limits(concurrent_opens=8, max_waiting=500))  # действует на следующие решения
```

Рост `resize` применяет сразу, а при уменьшении лишние браузеры дорабатывают и закрываются. Ничего не
перезапускается: выданные аренды дорабатывают как есть.

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`globe` Прокси
:link: proxies
:link-type: doc

Постоянный адрес у аккаунта, пауза для мёртвых прокси, часовой пояс по стране.
:::

:::{grid-item-card} {octicon}`code` Справочник: пул и аренда
:link: ../reference/api/pool
:link-type: doc

`BrowserPool`, `PageLease` и `ContextLease` со всеми методами.
:::
::::
