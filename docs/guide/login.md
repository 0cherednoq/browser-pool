# Вход

Один вход на аккаунт, без гонок между задачами.

Пять задач одного аккаунта стартуют одновременно, каждая видит «не залогинен» и идёт входить. Сайт получает пять
входов по паролю за секунду и отвечает капчей или баном, а если все пять вошли, сессии перезаписывают куки друг
друга. Обычно это закрывают замком на аккаунт и флагом «уже вошёл», но после перезапуска скрипта все аккаунты всё
равно входят по паролю заново. Отдельная проблема - двадцать аккаунтов, которые одновременно входят через один прокси:
сайт легко связывает их между собой.

## Один вход на аккаунт

Вход описывается в одном месте, в `SessionFlow.open`. Пул вызывает его, когда контексту аккаунта нужна сессия, и
следит за тремя вещами:

- На аккаунт идёт один вход. Пока работает `open`, остальные заявки этого аккаунта ждут и получают уже залогиненный
  контекст. Если вход не удался, ошибку получают все ждущие сразу, по очереди они входить не пробуют.
- Число одновременных входов ограничено и во всём пуле, и на один прокси.
- Сессия сохраняется сразу после входа и восстанавливается при следующем открытии: пароль не вводится повторно ни
  после падения браузера, ни после перезапуска процесса.

Это можно проверить без браузера, на фейковом драйвере. Вход в примере длится секунду:

```python
import asyncio

from browser_pool import BaseFlow, BrowserPool, Identity, OpenRequest, PoolConfig, Topology
from browser_pool.testing import FakeDriver


class CountingLoginFlow(BaseFlow):
    """Вход на сайт: считает, сколько раз пул его позвал."""

    def __init__(self) -> None:
        self.logins = 0

    async def open(self, ctx: OpenRequest) -> str:
        self.logins += 1
        await asyncio.sleep(1)  # форма, капча, редиректы
        return ctx.identity.key


async def main() -> None:
    flow = CountingLoginFlow()
    account = Identity(key="mail:42")
    config = PoolConfig(topology=Topology(pages_per_identity=5))

    async with BrowserPool(FakeDriver(), config=config, flow=flow) as pool:

        async def check_inbox() -> str:
            async with pool.page(account) as lease:
                return lease.session  # то, что вернул open

        results = await asyncio.gather(*(check_inbox() for _ in range(5)))

    print(results)  # ['mail:42', 'mail:42', 'mail:42', 'mail:42', 'mail:42']
    print(flow.logins)  # 1


asyncio.run(main())
```

На пять задач пришёлся один вход, и всё заняло около секунды. Задача просит вкладку аккаунта и получает её уже
залогиненной, поэтому замки и флаги в её коде не нужны.

## Как выглядит настоящий вход

```python
from dataclasses import dataclass, field

from browser_pool import BaseFlow, OpenRequest


@dataclass(frozen=True)
class MailAccount:
    login: str
    password: str = field(repr=False)


class MailLoginFlow(BaseFlow):
    async def open(self, ctx: OpenRequest) -> str:
        account: MailAccount = ctx.identity.payload
        page = await ctx.new_page()  # временная вкладка, пул закроет её сам
        await page.goto("https://mail.example/inbox")
        if ctx.restored and await page.locator("#inbox").count():
            return account.login  # сессия жива, пароль не нужен
        await page.fill("#login", account.login)
        await page.fill("#password", account.password)
        await page.click("button[type=submit]")
        await page.wait_for_selector("#inbox")
        await ctx.save_state()  # сохранить сразу, не дожидаясь периодического сохранения
        return account.login
```

`ctx.restored` показывает, восстановил ли пул сохранённую сессию в контекст. Креды лежат в `payload` identity и не
попадают в `repr`, логи и события пула. Подробнее про flow, прогрев вкладок и про то, кто хранит сессию, написано на странице
[Как написать SessionFlow](../extending/flow.md).

## Сколько входов идёт одновременно

```python
from browser_pool import PoolConfig
from browser_pool.config import Limits, Timeouts

PoolConfig(
    limits=Limits(
        concurrent_opens=4,  # входов разом во всём пуле
        concurrent_opens_per_proxy=1,  # входов разом через один прокси (по умолчанию 1)
    ),
    timeouts=Timeouts(open=120),  # вход дольше: сбой, а не вечное ожидание
)
```

Двадцать аккаунтов при `concurrent_opens=4` входят по четыре, и через один прокси два входа одновременно не идут.
Остальные задачи в это время не простаивают: аккаунты, которые уже вошли, работают.

## Вход не удался

Исключение из `open` означает неудачный вход. Аккаунт встаёт на паузу, которая удваивается с каждой неудачей подряд:
30 с, 60 с и так до 15 минут (`Recovery.open_failure_backoff`). Пул не повторяет вход по паролю в цикле, а `any_of`
в это время выдаёт другие аккаунты. Если пароль не подошёл или аккаунт забанен, сообщите пулу вид `blocked`: аккаунт
выйдет из работы до `pool.unblock(key)` (см. [Сбои и повторы](recovery.md)).

## Сессия переживает перезапуск

```python
from pathlib import Path

from browser_pool import BrowserPool
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.state import FileStateStore

pool = BrowserPool(
    PlaywrightDriver(),
    flow=MailLoginFlow(),
    state_store=FileStateStore(Path("sessions")),  # файл JSON на аккаунт, запись атомарная
)
```

Пул сохраняет куки и localStorage сразу после входа, раз в 5 минут (`Lifecycle.state_save_interval`) и при закрытии
контекста. После падения браузера новый контекст получит сохранённую сессию, после перезапуска процесса тоже.
Чтобы хранить сессии в своей БД, а не в файлах, возьмите `CallbackStateStore`. Запись защищена версией: два процесса
не перезапишут сессию друг друга незаметно.

## Сессия, которую принесли руками

Если оператор залогинился в своём браузере и прислал куки, конвертировать их не нужно:

```python
from pathlib import Path

from browser_pool import Identity, StatePolicy
from browser_pool.state import SessionState

session = SessionState.parse(Path("from_operator.txt").read_text())
account = Identity(key="mail:42", state=StatePolicy(initial=session))
```

`SessionState.parse` определяет формат по содержимому:

- `auth.json` Playwright;
- JSON-массив кук из расширения браузера;
- Netscape `cookies.txt`;
- строка `name=value; name2=value2` из DevTools.

:::{note}
`initial` используется, только пока в хранилище пусто; дальше пул ведёт сессию сам.
:::

## Несколько процессов на один аккаунт

Внутри одного пула аккаунт всегда живёт ровно в одном контексте. Но если скрипт запущен дважды или пулов несколько,
два процесса войдут в один аккаунт, и сайт разлогинит обоих. От этого защищает замок identity:

```python
from browser_pool import BrowserPool, FileIdentityLock
from browser_pool.drivers.playwright import PlaywrightDriver

pool = BrowserPool(
    PlaywrightDriver(),
    flow=MailLoginFlow(),
    identity_lock=FileIdentityLock("locks"),  # все процессы на этой машине
)
```

| Замок | Кто делит аккаунты |
|---|---|
| `LocalIdentityLock` (по умолчанию) | пулы одного процесса, которым передан один и тот же замок |
| `FileIdentityLock` | все процессы одной машины; если процесс умер, даже от `kill -9`, замок свободен сразу |
| свой класс с `acquire` / `release` | несколько машин: advisory lock PostgreSQL, Redis |

Если аккаунт занят другим процессом дольше `Timeouts.open`, заявка получает `IdentityBusyError`, аккаунт уходит на
паузу, а остальные работают.

:::{note}
Профиль Chrome на диске (`StatePolicy.user_data_dir`, драйвер pydoll) защищён таким же файловым замком
автоматически.
:::

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`stack` Параллельность
:link: parallelism
:link-type: doc

Сколько браузеров, вкладок и аккаунтов работает одновременно и кто ждёт в очереди.
:::

:::{grid-item-card} {octicon}`code` Справочник: состояние сессии
:link: ../reference/api/state
:link-type: doc

`SessionState`, `FileStateStore`, `CallbackStateStore` и протокол хранилища.
:::
::::
