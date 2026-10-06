# Сбои и повторы

Вы называете, что сломалось, а пул чинит ровно это и повторяет задачу.

TimeoutError посреди задачи может значить что угодно: умер прокси, протухла сессия, упал браузер или сайт просто
медленный. Каждый `except` решает это по-своему: где-то закрывает вкладку, где-то перелогинивается, где-то
перезапускает браузер, где-то просто повторяет. Обработка ошибок становится длиннее самой автоматизации, упавший
Chrome всё равно останавливает весь скрипт, а повтор рано или поздно отправляет одно письмо дважды.

## Виды сбоев и реакции

От вас пулу нужно знать только, что именно сломалось. Каждый сбой он сводит к одному из видов (`ErrorKind`) и сам
на него реагирует:

| Вид | Что сломалось | Что делает пул |
|---|---|---|
| `page` | вкладка | закрывает вкладку, следующая задача получит новую |
| `session` | вход протух | закрывает контекст, следующая задача откроет его заново через `open` |
| `proxy` | прокси | сообщает источнику прокси, переоткрывает контекст с другим прокси |
| `rate_limited` | сайт просит подождать | ставит аккаунт на паузу (на `retry_after` или 60 с), контекст жив |
| `blocked` | аккаунт забанен, пароль не подходит | выводит аккаунт из работы до `pool.unblock(key)` |
| `browser` | браузер упал | карантин и перезапуск браузера; его аккаунты восстановят сессии |
| `unknown` | непонятно | закрывает вкладку |

Сбои браузера, вкладки и прокси пул распознаёт сам, по ошибкам SDK браузера. О сбоях сайта (бан, протухшая сессия,
лимит запросов) ему сообщаете вы, одним из трёх способов.

Первый способ - функция `classifier`. Она подходит, когда SDK сайта - отдельная библиотека и о пуле не знает:

```python
from browser_pool import BrowserPool, ErrorKind
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.errors import Classification


class AccountBannedError(Exception): ...


class SessionExpiredError(Exception): ...


class TooManyRequestsError(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"повторить через {retry_after} с")
        self.retry_after = retry_after


def classify(error: BaseException) -> ErrorKind | Classification | None:
    match error:
        case AccountBannedError():
            return ErrorKind.blocked
        case SessionExpiredError():
            return ErrorKind.session
        case TooManyRequestsError(retry_after=seconds):
            return Classification(ErrorKind.rate_limited, seconds)
    return None  # не моё, решит драйвер


pool = BrowserPool(PlaywrightDriver(), classifier=classify)
```

Второй - атрибут на своём исключении. Зависимость от пула для этого не нужна:

```python
class SessionExpiredError(Exception):
    pool_error_kind = "session"
```

Третий - `PoolSignal`, для кода, который и так знает о пуле:

```python
from browser_pool import ErrorKind, PoolSignal

async with pool.page(account) as lease:
    if "login" in lease.page.url:
        raise PoolSignal("выкинуло на страницу входа", kind=ErrorKind.session)
```

Ошибку, завёрнутую в другую (`raise OperationFailed(...) from error`), пул разворачивает, и её вид не теряется.

:::{note}
Исключение задачи всегда доходит до вас как есть: пул на него реагирует, но не перехватывает.
:::

## Повторы без try/except

```python
from browser_pool import Backoff

result = await pool.run(check_inbox, account, retries=3, backoff=Backoff.exp(1, 30))
```

Каждая попытка идёт в новой аренде. К этому моменту пул уже отреагировал на сбой, поэтому следующая попытка получает
исправное: новую вкладку, новый контекст, другой прокси или перезапущенный браузер. Повторяются только сбои окружения
(`page`, `session`, `proxy`, `browser`). Задача, упавшая из-за бага в вашем коде или бана аккаунта, не повторяется. Паузы растут с
разбросом, чтобы задачи, упавшие одновременно, не повторялись тоже одновременно.

## Повтор, который не отправит письмо дважды

:::{warning}
Вкладка может упасть уже после того, как письмо ушло, и тогда повтор отправит дубль. Перед необратимым действием
вызовите `lease.commit()`: эта попытка больше не повторится.
:::

```python
async def send_invoice(lease) -> None:
    await lease.page.goto("https://mail.example/compose")  # упадёт здесь, повторим
    await lease.page.fill("#to", "client@example.com")
    lease.commit()  # дальше письмо могло уйти
    await lease.page.click("#send")


await pool.run(send_invoice, account, retries=3)
```

## Браузер упал посреди работы

От приложения здесь ничего не требуется. Браузер уходит в карантин: новых задач не получает, а его задачи падают с
видом `browser` и повторяются (`pool.run`) в других браузерах. Пул закрывает остатки и запускает браузер заново; если
запуск не удался, пауза перед следующей попыткой растёт. Аккаунты открываются в новом браузере с сохранёнными
сессиями, без входа по паролю. Проверка здоровья раз в 15 секунд находит и зависший браузер, который не упал, а
перестал отвечать.

Это показывает пример [`examples/accounts`](../../examples/accounts): посреди работы он убивает процесс одного
браузера, а в итоге вся работа сделана и у каждого аккаунта ровно один вход по паролю.

## Аккаунт на паузе

```python
pool.cool_down(account, 600)  # своя причина подождать
pool.identity_status(account)  # cooling_until, blocked, open_failures
pool.block(account, "ручная проверка")
pool.unblock(account)  # снимает и паузу, и блокировку
```

Пока аккаунт на паузе, `any_of` его пропускает. Заявка с `wait_cooldown=False` не ждёт конца паузы, а сразу получает
`IdentityCoolingDownError`, и задачу можно вернуть в свою очередь. Если у приложения свой учёт пауз аккаунтов, решите, кто главный, пул или приложение: об этом
раздел [«Две паузы у одного аккаунта»](../extending/flow.md#две-паузы-у-одного-аккаунта).

## Улики

От задачи, упавшей ночью, утром остаётся только traceback: страницы уже нет. Приёмник улик снимает с упавшей вкладки
скриншот, HTML и адрес до того, как пул её закроет:

```python
from pathlib import Path

from browser_pool import BrowserPool
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.evidence import DirectoryEvidenceSink

pool = BrowserPool(PlaywrightDriver(), evidence_sink=DirectoryEvidenceSink(Path("evidence")))
```

В каталоге появляются файлы `<время>-<аккаунт>-<аренда>.png`, `.html`, `.json`; ссылка на них есть в событии
`LeaseReleased`. Свой приёмник (S3, баг-трекер) - это класс с методом `save` по протоколу `EvidenceSink`. Сбой съёмки
не роняет задачу.

## Вкладка испорчена, но исключения нет

```python
async with pool.page(account) as lease:
    ...
    lease.discard_page()  # закрыть вкладку, а не вернуть тёплой
    lease.report(ErrorKind.session)  # или прямо сказать, что сломалось
    await lease.retire_context("сайт сменил язык интерфейса")  # контекст на выход после этой аренды
```

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`cpu` Ресурсы хоста
:link: resources
:link-type: doc

Пороги памяти и CPU, перезапуск распухших браузеров, ни одного Chrome после остановки.
:::

:::{grid-item-card} {octicon}`book` Виды сбоев
:link: ../explanation/errors
:link-type: doc

Вид сбоя называет ресурс, которому больше нельзя доверять.
:::
::::
