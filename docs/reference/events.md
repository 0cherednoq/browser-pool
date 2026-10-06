# События

Страница сгенерирована из `src/browser_pool/events.py` скриптом `scripts/reference_tables.py`, руками её не правят.

Подписка идёт через `pool.on(EventType, handler)`; подписка на `PoolEvent` получает все события. У каждого события
есть поле `at`, момент в UTC. В полях только несекретное: ключи identity, номера аренд, браузеры, виды сбоев, имена
типов исключений. Как пользоваться событиями, описано на странице [Наблюдение](../guide/observability.md).

## Браузеры

| Событие | Что случилось | Поля |
|---|---|---|
| `BrowserStarted` | Процесс браузера запущен (впервые, после простоя или восстановления). | `browser_id` (`str`) |
| `BrowserQuarantined` | Браузер умер или завис и выведен из работы целиком. | `browser_id` (`str`)<br>`reason` (`str`) |
| `BrowserRestarted` | Браузер снова в строю после карантина или планового перезапуска. | `browser_id` (`str`) |
| `BrowserRecycled` | Начат плановый перезапуск браузера. | `browser_id` (`str`)<br>`reason` (`str`): `leases` - по числу аренд, `uptime` - по времени жизни, `rss` - распух (`Resources`). |
| `BrowserIdleClosed` | Простаивающий браузер закрыт; понадобится - запустится снова. | `browser_id` (`str`) |
| `PoolUnavailable` | Все браузеры в карантине, восстановление не идёт: ждать нечего. |  |

## Контексты и identity

| Событие | Что случилось | Поля |
|---|---|---|
| `ContextOpened` | Контекст identity открыт физически. | `key` (`str`)<br>`browser_id` (`str`)<br>`generation` (`int`) |
| `ContextRetired` | Контекст выведен из работы явно: арендатором или реакцией на сбой. | `key` (`str`)<br>`generation` (`int`)<br>`reason` (`str`) |
| `ContextClosed` | Контекст закрыт физически - по любой причине, включая вытеснение и простой. | `key` (`str`)<br>`generation` (`int`) |
| `SessionOpened` | `flow.open` прошёл: сессия identity открыта. | `key` (`str`)<br>`restored` (`bool`): в контекст было что восстановить - сохранённое или начальное состояние. |
| `StateSaved` | Состояние сессии сохранено в хранилище. | `key` (`str`)<br>`version` (`int`)<br>`trigger` (`str`): `open` - сразу после входа, `interval` - периодически, `close` - перед закрытием, `flow` - по просьбе flow. |
| `StateExportFailed` | Состояние не снялось с контекста (обычно - контекст уже мёртв); хранилище не тронуто. | `key` (`str`)<br>`error` (`str`) |
| `StateSaveFailed` | Состояние не записалось в хранилище; сессия в контексте жива, работа продолжается. | `key` (`str`)<br>`error` (`str`): имя типа исключения хранилища.<br>`trigger` (`str`): когда сохраняли - как у `StateSaved.trigger`. |
| `OpenFailed` | Контекст identity не открылся; identity на паузе. | `key` (`str`)<br>`error` (`str`): имя типа исключения.<br>`retry_in` (`float`): через сколько секунд identity снова получит попытку. |
| `ResourcePressure` | Хост вошёл под давление памяти или CPU (`reason`) или вышел из него (`reason=None`). | `reason` (`str \| None`): что давит; `None` - давление снято, пул снова растёт.<br>`free_memory_mb` (`float \| None`)<br>`cpu_percent` (`float \| None`): средняя загрузка за окно `Resources.sample_window`. |
| `TaskRetried` | `pool.run`: попытка задачи не удалась, будет следующая - с новой арендой. | `key` (`str`): identity упавшей попытки.<br>`attempt` (`int`): номер упавшей попытки, с единицы.<br>`kind` (`ErrorKind`): вид сбоя, по которому решено повторить.<br>`error` (`str`): имя типа исключения.<br>`delay` (`float`): пауза перед следующей попыткой, секунды. |
| `IdentityCooledDown` | Identity поставлена на паузу. | `key` (`str`)<br>`seconds` (`float`) |
| `IdentityBlocked` | Identity заблокирована до явного `unblock`. | `key` (`str`)<br>`kind` (`ErrorKind \| None`): почему: вид сбоя; `None` - заблокировали вручную (`pool.block`). |
| `OrphansReaped` | При старте добиты браузеры, которые пережили свой пул (процесс приложения умер). | `count` (`int`) |

## Окна

| Событие | Что случилось | Поля |
|---|---|---|
| `WindowPlaced` | Окно встало на место: в ячейку сетки или свёрнуто, потому что ячеек не хватило. | `key` (`str`): чьё окно: ключ identity или `identity#вкладка` при окне на вкладку.<br>`slot` (`int \| None`): номер ячейки; `None` - окно свёрнуто.<br>`x` (`int`)<br>`y` (`int`)<br>`width` (`int`)<br>`height` (`int`) |

## Прокси

| Событие | Что случилось | Поля |
|---|---|---|
| `ProxyFailed` | Прокси не пропустил identity: источнику ушёл отчёт, контекст закрывается. | `key` (`str`)<br>`proxy` (`str`): безопасное имя прокси (`Proxy.label`), без кредов.<br>`reason` (`str`): имя типа исключения или вид сбоя.<br>`retrying` (`bool`): будет ли открытие повторено с другим прокси. |
| `NoUsableProxy` | Identity нужен прокси, а источник не дал ни одного пригодного. | `key` (`str`) |

## Аренды

| Событие | Что случилось | Поля |
|---|---|---|
| `LeaseAcquired` | Вкладка выдана. | `lease_id` (`int`)<br>`key` (`str`)<br>`browser_id` (`str`)<br>`generation` (`int`)<br>`waited` (`float`): сколько секунд заявка ждала выдачи. |
| `LeaseReleased` | Аренда завершена. | `lease_id` (`int`)<br>`key` (`str`)<br>`browser_id` (`str`)<br>`held` (`float`): сколько секунд вкладка была в аренде.<br>`outcome` (`ErrorKind \| None`): вид сбоя, если аренда кончилась сбоем; `None` - штатно.<br>`error` (`str \| None`): имя типа исключения арендатора, если оно было.<br>`evidence` (`str \| None`): ссылка на улики сбоя от `EvidenceSink` (путь, идентификатор); `None` - не снимались. |
| `AcquireWatchdog` | Заявка ждёт выдачи дольше `acquire_watchdog`; ожидание продолжается. | `waited` (`float`)<br>`candidates` (`tuple[str, ...]`)<br>`leases_active` (`int`)<br>`waiting` (`int`) |
| `LeakSuspected` | Аренда держится дольше `leak_warn_after` - возможно, её забыли вернуть. | `lease_id` (`int`)<br>`key` (`str`)<br>`held` (`float`)<br>`acquired_at` (`str`): стек места, где аренду взяли. |
| `LeaseRevoked` | Аренда дольше `lease_max_duration`: задача-держатель отменена. | `lease_id` (`int`)<br>`key` (`str`)<br>`held` (`float`) |
| `PoolHealth` | Снимок пула после очередной проверки здоровья - для публикации ёмкости наружу. | `snapshot` (`PoolSnapshot`) |
