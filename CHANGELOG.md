# Changelog

Формат - [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии - [SemVer](https://semver.org/lang/ru/).

До 1.0 публичный API и протокол `Driver` могут меняться в минорных версиях (с 0.x на 0.x+1); каждое такое изменение
отмечается здесь в разделе «Изменено» с указанием, что сделать при обновлении. Патч-версии совместимы. С 1.0 протокол
`Driver`, `EndpointProvider`, `SessionFlow` и публичные имена `browser_pool` меняются только в мажорных версиях.

## [0.1.0a1] - не выпущена

Первая альфа: ядро пула, два драйвера, провайдеры эндпоинтов, высокоуровневый API. Ставится с `pip install --pre`.
Публичный API - то, что экспортирует `browser_pool`, и подпакеты `drivers`, `providers`, `proxies`, `state`, `events`,
`testing` (границу проверяет `tests/test_public_api.py`); `browser_pool._core` - внутреннее.

### Пул и аренда

- `BrowserPool` поверх любого драйвера: аренда вкладки (`pool.page`) и контекста целиком (`pool.context`), `any_of`,
  приоритеты, `acquire_timeout`, `wait_cooldown`, очередь без блокировки головы, `max_waiting`. Одинаковые параметры
  ожидания у `page`, `context`, `run` и `map`.
- `pool.run` / `pool.map`: повтор по виду сбоя с `Backoff`. `lease.commit()` - точка невозврата перед необратимым
  действием (письмо, платёж): после неё эта попытка не повторяется.
- Отзыв аренды по `Limits.lease_max_duration` - типизированная `LeaseRevokedError` на границе `async with`;
  `driver.prepare()`, не уложившийся в `Timeouts.startup`, - `StartupTimeoutError`; `ProxyPolicy.sticky()` без
  `proxy_source` - предупреждение при первой такой аренде (аренда пойдёт с адреса хоста). Карантин браузера не вечен: слот, исчерпавший `Recovery.restart_max_attempts`, пробуется снова из
  проверки здоровья.
- Тёплые вкладки, контексты identity с лимитами (`Topology`, `Limits`, `GroupLimit`), варианты identity,
  `pool.resize` / `pool.reconfigure` на лету.
- Аренда не теряется при отмене: хвост (улики, хук `before_release`, сброс вкладки) идёт задачей пула, слот и вкладка
  возвращаются, `pool.map` с медленным `evidence_sink` и `asyncio.timeout` снаружи не оставляют занятых слотов.
- Реакция на сбои по `ErrorKind` (`page`, `session`, `proxy`, `browser`, `blocked`, `rate_limited`): `lease.report`,
  `PoolSignal`, `classifier` пула и `classify` драйвера; пауза и блокировка identity (`pool.cool_down`, `block`,
  `unblock`, `identity_status` - принимают `Identity` или ключ). Вид читается по цепочке причин.
- Неудачное открытие контекста (запуск, `flow.open`) не повторяется арендами, выданными до сбоя: они получают ту же
  ошибку, пауза identity считается один раз. Исключение открытия несёт `pool_identity` и заметку с ключом - при `any_of`
  видно, какой кандидат сломался.
- Супервизор: карантин и восстановление браузеров, плановый перезапуск, закрытие простаивающих, `min_browsers`, страж
  процессов (после остановки Chrome не остаётся, сирот упавшего процесса подбирает следующий запуск).
- Типы: `PageLease[B, C, P, S]` несёт тип сессии `S` - задача, объявившая `PageLease[..., MailSession]`, получает
  типизированный `lease.session` (пул отдаёт `S = Any`: у разных identity flow разный).
- Хуки: `before_launch`, `after_browser_started`, `before_context`, `after_context_created`, `after_page_created`,
  `after_acquire`, `before_release`; плагин с методом, похожим на точку, но не точкой (опечатка), - `ConfigError`.

### Конфигурация

- `PoolConfig` из секций `Topology`, `Limits`, `Lifecycle` (простой, проверка здоровья), `Recycling` (плановая смена
  браузеров и контекстов), `Recovery` (паузы и повторы после сбоев, `Backoff`), `Timeouts`, `Windows`, `Debug`,
  `Resources`; пресеты `accounts()` и `scraping()`, `from_mapping` / `to_mapping`, `replace`.
  `replace(limits={"max_waiting": 50})` меняет только названные поля секции и не теряет остальные поля пресета;
  `replace(limits=Limits(...))` заменяет секцию целиком.
- Значения с `Literal`-полями проверяют их в рантайме: неизвестная строка - `ValueError` (в конфиге и возможностях -
  `ConfigError`) с перечнем допустимых.
- `min_size` окон по умолчанию 560×400 - не меньше минимального окна Chrome.

### Сессии, прокси, возможности драйвера

- `SessionFlow` / `BaseFlow`: вход, прогрев вкладок, сохранение состояния. `StateStore`: память, файлы
  (`FileStateStore` - имя файла из читаемой части ключа и хеша, ключи с разным регистром - разные записи; чтение и запись
  не мешают друг другу на Windows), колбэки (обычные и корутинные функции).
- Сбой `StateStore` при сохранении не роняет аренду и не заставляет входить заново: событие `StateSaveFailed`, запись
  в лог, сессия в контексте жива. Запись в хранилище ограничена `Timeouts.state_export`.
- `SessionState.parse`: `auth.json` Playwright, JSON-массив кук, Netscape `cookies.txt`, строка кук; нечитаемое -
  только `StateFormatError`.
- Прокси: `Proxy.parse` (виды `host:port`, `host:port:user:pass`, `user:pass:host:port`, `user:pass@host:port`,
  `host:port@user:pass`, `scheme://…`; неоднозначная строка отвергается, ошибка не содержит фрагментов строки; хост в
  нижнем регистре), `ProxyList` (стратегии, выключатель - один инцидент даёт одно срабатывание; `host:port` с разными
  логинами - разные прокси; `status()`, `ban()`, `add()`, `remove()`; в `parse_lines` - номер строки в ошибке и
  `skip_invalid`), `CallbackProxySource`, `TieredProxyList` (сбои открытия поднимают уровень), `HttpGeoChecker` (часовой
  пояс, локаль и геопозиция по выходу прокси; сбой - `None` с коротким кэшем).
- `ProxyPolicy.sticky()` закрепляет прокси за identity независимо от `StatePolicy` и хранилища (источник выбирает по ключу
  identity); `ProxyList(strategy="sticky")` - стратегия списка для всех identity, а не закрепление.
- Планирование по `DriverCapabilities`: прокси на контексте, на браузере (браузер принадлежит владельцу) или у вендора;
  `effective_config`; `UnsupportedRequirementError` при аренде; поток на браузер для синхронных SDK (`lease.call`,
  экспериментально).

### Драйверы и провайдеры

- `PlaywrightDriver` (extra `[playwright]`, Playwright 1.55 и новее; проверен Chromium, Firefox и WebKit не проверялись),
  `PydollDriver` (extra `[pydoll]`, pydoll от 2.27 и ниже 4; проверены 2.27 и 3.0). У обоих `headless=` и публичный
  `driver.headless` (`None` - решает пул; `True` выключает секцию окон). Прокси с логином: вкладка отвечает на
  авторизацию сама, отвергнутые креды - вид `proxy`, `socks5` с логином отвергается при аренде
  (`DriverCapabilities.proxy_auth_schemes`). Куки с любыми атрибутами сохраняются; куку, которую модель не представляет,
  экспорт пропускает с записью в лог.
- Свой драйвер: `browser_pool.driver.BaseDriver` - обязательны `capabilities`, `launch`, `ping`, `close_browser`,
  `new_context`, `close_context`, `new_page`, `page_usable`, `close_page`; остальное по умолчанию «не умею».
  Возможности сверяются с реализацией при создании пула (`ConfigError` со всеми расхождениями разом), настройки
  контекста и отладочные опции, которых драйвер не применяет (`DriverCapabilities.context_settings`, `debug_options`),
  дают отказ при аренде (`UnsupportedRequirementError`) или предупреждение при старте, а не тихий пропуск. Приложение
  проверяет драйвер `DriverContractSuite`.
- Провайдеры эндпоинтов (`from browser_pool.providers import …`): `RemoteCDP` (уже запущенные браузеры; токен в
  адресе сохраняется в запросе и в `wss://`), `AdsPowerProvider` (профили антидетекта; профиль, запущенный до отмены
  или сбоя `start`, закрывается; сироты - по владельцу-процессу в реестре).
- Профили на диске: `StatePolicy(user_data_dir=…, profile_template=…)` (пути можно задавать и строкой) - свой браузер
  на профиль, файловый замок на время жизни браузера, копирование образца; `Recycling.persistent_browser_max_leases`.
  `FileIdentityLock` - эксклюзивность identity между процессами одного хоста.

### Наблюдаемость и отладка

- `pool.snapshot()`, шина событий (`pool.on`), `PoolHealth`, сторож долгих аренд, улики сбоев (`EvidenceSink`;
  `DirectoryEvidenceSink` - комплект файлов, ссылка - путь к `.json`), метрики Prometheus (extra `[prometheus]`; метка
  `pool`, `attach(pool, name=…)` на каждый пул), защита хоста `Resources` (extra `[resources]`; память дерева браузера
  считается без повторного учёта общих страниц).
- Окна для отладки (`Windows`): раскладка без перекрытий, подписи окон, `hold_on_error` (`pool.held_pages`), `slow_mo`.
  Секция выключается с предупреждением, когда показать окна негде (драйвер headless, нет экрана).

### Тесты

- `browser_pool.testing`: `FakeDriver`, `FakeEndpointProvider`, `FakeHostProbe`, `VirtualTimeLoop`, публичный
  `DriverContractSuite`, плагин pytest (extra `[testing]`: pytest и pytest-asyncio не ниже 1.4). Виртуальное время включает
  метка `virtual_time` (без неё тест идёт на обычном цикле и обычном времени); `fake_driver` проверяет, что тест ничего
  не оставил.

### Примеры и документация

- `examples/quotes` (живой сайт, Playwright) и `examples/accounts` (локальный сайт, Playwright и pydoll): SDK сайта -
  отдельный слой без `browser_pool`, оркестрация - в `app`; правило слоёв проверяется тестом.
- Сайт документации (Sphinx, `uv run poe site`): начало работы на трёх запускаемых примерах (`examples/start`),
  словарь понятий, руководства по задачам, SessionFlow (в том числе кто владеет сессией и две паузы у аккаунта),
  свой драйвер и провайдер, устройство пула, справочники конфига, событий, ошибок и API, файл llms.txt для агентов.
  Имена из документации проверяются тестом на существование в коде, справочник API - на полноту, вывод примеров -
  на совпадение с настоящим, оформление страниц - отдельным тестом.

### Поддержка

- Python 3.12, 3.13, 3.14. Проверено на Windows; Linux - в CI (матрица Linux и Windows).
