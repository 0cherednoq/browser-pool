# Как написать SessionFlow

Вход на сайт и подготовка вкладок, описанные один раз.

Через `SessionFlow` пул просит приложение сделать контекст рабочим: открыть сессию identity и подготовить её вкладки.
Как именно это делается, пул не знает. За ним остаются контексты, прокси, восстановление после падений и реакции на
сбои.

```python
class SessionFlow[C, P, S](Protocol):
    async def open(self, ctx: OpenRequest[C, P]) -> S: ...        # открыть сессию, вернуть её объект
    async def prepare_page(self, session: S, page: P) -> None: ...  # прогрев новой вкладки, один раз
    async def reset_page(self, session: S, page: P) -> bool: ...    # перед возвратом в запас; False: закрыть вкладку
    async def close(self, session: S) -> None: ...                  # контекст закрывается
```

`C`, `P` - контекст и вкладка вашего SDK (у Playwright - `BrowserContext` и `Page`). `S` - любой объект, который вам
удобно держать как «сессию»: логин, адрес ящика, клиент API. Проще наследовать `BaseFlow`: в нём обязателен только
`open`.

:::{note}
Это тот же протокол фабрики, что у пулов соединений (`makeObject`/`activate`/`passivate` в Commons Pool,
`configure`/`reset` в psycopg_pool): пул знает, когда позвать метод, но не знает, что внутри.
:::

## Где живёт код сайта

Селекторы, формы входа, признаки протухшей сессии относятся к SDK сайта. Flow пишется на слое, который собирает пул и
SDK вместе, и остаётся тонким адаптером: достаёт креды из `payload`, отдаёт SDK контекст и возвращает то, что SDK
построил.

```python
class MailFlow(BaseFlow[BrowserContext, Page, MailSession]):
    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions

    async def open(self, ctx: OpenRequest[BrowserContext, Page]) -> MailSession:
        account: Account = ctx.identity.payload
        page = await ctx.new_page()                 # временная вкладка, пул закроет её сам
        client = mail_sdk.client(page, account, store=self.sessions)
        await client.sign_in()                      # SDK решает: принять сохранённую сессию или войти
        return MailSession(account=account)

    async def prepare_page(self, session: MailSession, page: Page) -> None:
        await mail_sdk.client(page, session.account, store=self.sessions).open_inbox()
```

Так устроены оба примера - [`examples/quotes`](../../examples/quotes) и [`examples/accounts`](../../examples/accounts):
SDK сайта (`quotes_sdk`, `mail_sdk`) не импортирует `browser_pool`, а flow, классификатор и сборка пула живут в `app`.
Это правило проверяет тест (`tests/test_examples.py`): он падает, если SDK импортирует пул.

Что есть в `ctx`:

| Имя | Что это |
|---|---|
| `identity` | чья сессия, вместе с `payload` |
| `context` | нативный контекст |
| `restored` | что пул восстановил в контекст; `None`, если ничего |
| `proxy` | прокси контекста, если он есть |
| `new_page()` | временная вкладка для входа, пул закроет её после `open` |
| `save_state()` | сохранить состояние сразу |
| `call(fn, *args)` | синхронный код входа в потоке браузера для синхронных драйверов |

## Кто владеет сессией

Состояние сессии (куки, localStorage) может хранить пул или SDK. Выберите одного владельца, иначе у двух хранилищ
окажутся разные версии сессии, и будет неясно, какая свежее.

### Пул

`StatePolicy(mode="read_write")`, режим по умолчанию. Перед `open` пул восстанавливает состояние в контекст из
`StateStore` или из `StatePolicy.initial`; `ctx.restored` показывает, было ли что восстанавливать. `open` проверяет,
жива ли сессия, и входит, только если нет. После успешного `open` пул сохраняет состояние сам, а дальше периодически
(`Lifecycle.state_save_interval`) и при закрытии контекста. `ctx.save_state()` сохраняет сразу, не дожидаясь очередного сохранения.

Этот режим подходит, когда у SDK нет своего хранилища.

### SDK

`Identity(..., state=StatePolicy(mode="none"))`. Пул открывает чистый контекст и не трогает `StateStore`. Сессию в
контекст кладёт SDK в `open` (из своего хранилища или входом), и он же решает, когда перелогиниться.

Этот режим подходит, когда у SDK свой жизненный цикл сессии (ревизии, общий доступ с HTTP-клиентом). Так же устроены
облачные браузеры: Browserbase и Steel хранят профиль, а вход остаётся за пользователем.

### Профиль на диске

`StatePolicy(mode="none", user_data_dir=Path("profiles/42"))`, нужен драйвер с `persistent_dir` (pydoll). Сессию
хранит сам браузер: куки, localStorage, IndexedDB, расширения - всё, что лежит в профиле Chrome. У identity свой
браузер, запущенный с этим профилем, прокси задаётся при запуске, а контекстом служит готовый контекст профиля
(`ctx.context`).

- Настройки контекста (`Identity.context_options`: локаль, часовой пояс, геопозиция, viewport, user agent) к готовому
  контексту не применяются. Identity, которая их задала, получает при аренде `UnsupportedRequirementError`, а не
  контекст без этих настроек.
- Пока жив браузер профиля, профиль занят файловым замком (`profiles/42.lock`). Второй процесс или вторая identity с
  тем же каталогом ждут не дольше `Timeouts.open` и получают `IdentityBusyError`. Если процесс упал, замок
  освобождается сразу.
- `profile_template` - каталог-образец, который копируется в профиль перед первым запуском.
- Плановый перезапуск по числу аренд для таких браузеров выключен (`Recycling.persistent_browser_max_leases`).
- Хранилище пула с профилем обычно не нужно, отсюда `mode="none"`.

Во всех трёх режимах сбой `session` у аренды выводит контекст из работы, и следующая аренда снова позовёт `open`.

## Ошибки

Исключение из `open` означает неудачное открытие: identity встаёт на паузу, которая с каждой неудачей удваивается
(`Recovery.open_failure_backoff`), и следующая заявка попробует снова. К исключению открытия пул добавляет атрибут
`pool_identity` (ключ identity) и заметку через `add_note`, чтобы при `any_of` было видно, какой кандидат сломался.
Тип исключения не меняется.

Исключение из аренды пробрасывается наружу как есть.

В обоих случаях пул сначала выясняет, что именно сломалось (`ErrorKind`), и по виду сбоя решает, что делать с
ресурсами:

| Вид            | Реакция                                                                              |
|----------------|--------------------------------------------------------------------------------------|
| `page`         | закрыть вкладку                                                                      |
| `session`      | вывести контекст, следующий откроется через `open`                                   |
| `proxy`        | отчёт источнику, контекст с другим прокси (до `Recovery.proxy_retries` раз)          |
| `rate_limited` | пауза identity на `retry_after` (или `Recovery.rate_limited_cooldown`), контекст жив |
| `blocked`      | блок до `pool.unblock(key)`, заявки получают `IdentityBlockedError` сразу            |
| `browser`      | карантин и перезапуск браузера                                                       |
| `unknown`      | закрыть вкладку                                                                      |

### Как пул определяет вид сбоя

Пул спрашивает источники по порядку и берёт первый ответ:

1. `lease.report(kind, retry_after=…)` - арендатор сказал явно;
2. вид, объявленный самой ошибкой: `PoolSignal(kind=…)` или атрибут `pool_error_kind` (и `pool_retry_after`);
3. `classify` пула - `BrowserPool(classifier=…)`;
4. `classify` драйвера - ошибки SDK браузера: прокси, упавший процесс, закрытая вкладка;
5. `unknown`.

Шаги 2-4 проходят по цепочке причин снаружи внутрь (`__cause__`, иначе `__context__`, если его не скрыли `from None`),
поэтому вид ошибки не теряется, когда SDK заворачивает её в свою (`raise OperationFailed(...) from e`). На каждом шаге
приоритет у внешнего звена: тот, кто завернул ошибку, знает о ней больше. Если классификатор сам упал, считается, что
он не ответил; исходную ошибку его исключение не заменяет.

### Как сказать пулу, что случилось

Основной путь - `classify` на слое сборки. Он подходит, когда SDK сайта - отдельная библиотека: SDK бросает свои
доменные ошибки и о пуле не знает, а соответствие между ошибкой SDK и видом сбоя живёт там же, где flow:

```python
from browser_pool.errors import Classification

def classify(error: BaseException) -> ErrorKind | Classification | None:
    match error:
        case AccountBannedError() | WrongPasswordError():
            return ErrorKind.blocked
        case TooManyRequestsError(retry_after=seconds):
            return Classification(ErrorKind.rate_limited, seconds)   # пауза, которую назвал сайт
        case SessionExpiredError():
            return ErrorKind.session
    return None                                                      # не моё: дальше решает драйвер

pool = BrowserPool(driver, flow=MailFlow(sessions), classifier=classify)
```

Атрибут `pool_error_kind` нужен, когда SDK хочет подсказать вид сам, но не зависеть от пула. Пул прочитает любое
исключение с `pool_error_kind = "session"` (и `pool_retry_after` для `rate_limited`).

`PoolSignal` предназначен для кода, который и так знает о пуле: скрипт без SDK, flow, задача. Его можно бросить
напрямую, `raise PoolSignal("пароль не подошёл", kind=ErrorKind.blocked)`, или завести свой класс с видом по
умолчанию: `class Banned(PoolSignal): default_kind = ErrorKind.blocked`.

### Две паузы у одного аккаунта

Пул сам ставит identity на паузу в трёх случаях: после неудачного открытия (растущая пауза
`Recovery.open_failure_backoff`), по `rate_limited` (на `retry_after` или `rate_limited_cooldown`) и по
`pool.cool_down(key, seconds)`. Если у приложения есть свой учёт пауз аккаунта (очереди, лейны, квоты), у аккаунта
окажутся две паузы с разными сроками, и будет неясно, какой из них верить. Поэтому паузами должен управлять кто-то
один: либо пул, либо приложение.

Если паузами управляет пул, приложение своих пауз не держит, а узнаёт о них у пула: через
`pool.identity_status(key)` (`cooling_until`, `blocked`, `open_failures`) и события `IdentityCooledDown`, `OpenFailed`
(`retry_in`), `IdentityBlocked`. Заявка с `wait_cooldown=False` не ждёт конца паузы, а сразу получает
`IdentityCoolingDownError`, и задачу можно вернуть в свою очередь.

Если паузами управляет приложение, пул не должен добавлять свои:

- сигналы лимитов сайта `classify` отдаёт не как `rate_limited`, а как `None` (или вид без паузы), и решение о паузе
  принимает приложение;
- пауза после неудачного открытия выключается: `Recovery(open_failure_backoff=Backoff.none())`;
- о своей паузе приложение сообщает пулу через `pool.cool_down(key, seconds)`, чтобы `any_of` пропускал аккаунт. Пауза
  только продлевается: более короткая не сокращает уже назначенную. Досрочно её снимает `pool.unblock(key)`, вместе
  с блокировкой, если она есть.

Блокировка по виду `blocked` означает «аккаунт сломан», а не «подождать». Её снимают тем же `pool.unblock(key)`, когда
аккаунт починили.

## Вкладки: `prepare_page` и `reset_page`

`prepare_page` зовётся один раз для каждой новой вкладки: открыть рабочую страницу, закрыть баннеры. Дальше вкладка
считается тёплой, и следующая аренда получит её как есть. `reset_page` зовётся при возврате вкладки. Он возвращает её
в исходное состояние или отвечает `False`, если вкладка испорчена; тогда пул её закроет.

## Flow на identity

Обычно flow один на пул: `BrowserPool(flow=…)`. Отдельной identity можно дать свой: `Identity(key=…, flow=OtherFlow())`.
Payload в `repr` не попадает, поэтому креды можно класть туда.

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`plug` Как написать драйвер
:link: driver
:link-type: doc

Адаптер своего браузерного SDK и контрактные тесты к нему.
:::

:::{grid-item-card} {octicon}`code` Справочник: flow и хуки
:link: ../reference/api/flow
:link-type: doc

`SessionFlow`, `BaseFlow`, `OpenRequest` и точки хуков.
:::
::::
