# Как написать драйвер

Адаптер своего браузерного SDK и контрактные тесты к нему.

Драйвер - это адаптер браузерного SDK. Пул просит его запустить браузер, открыть контекст и вкладку и снять состояние,
больше от драйвера ничего не требуется. В поставке есть `PlaywrightDriver` и `PydollDriver` (`browser_pool.drivers`).
Эталоном поведения служит `browser_pool.testing.FakeDriver`: на нём ядро проверяет само себя.

## С чего начать: `BaseDriver`

Наследуйте `browser_pool.driver.BaseDriver[B, C, P]`, где `B`, `C`, `P` - браузер, контекст и вкладка вашего SDK.
Обязательны свойство `capabilities` и восемь методов. У остальных есть реализация по умолчанию, которая отвечает
«не умею».

```python
from typing import override

from browser_pool.driver import BaseDriver, ContextSpec, DriverCapabilities, LaunchSpec


class MyDriver(BaseDriver[Browser, Context, Page]):
    @property
    @override
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(proxy_scope="browser")   # что умеете, см. «Возможности»

    @override
    async def launch(self, spec: LaunchSpec) -> Browser:
        return await sdk.launch(headless=spec.headless, proxy=spec.proxy and spec.proxy.url)

    @override
    async def ping(self, browser: Browser) -> bool:
        return await browser.is_alive()

    @override
    async def close_browser(self, browser: Browser) -> None:
        await browser.quit()

    @override
    async def new_context(self, browser: Browser, spec: ContextSpec) -> Context:
        return browser.default_context()                   # без can_new_context: единственный

    @override
    async def close_context(self, context: Context) -> None:
        return None

    @override
    async def new_page(self, context: Context) -> Page:
        return await context.open_tab()

    @override
    def page_usable(self, page: Page) -> bool:
        return not page.closed

    @override
    async def close_page(self, page: Page) -> None:
        await page.close()
```

Такой драйвер уже работает в пуле и проходит контрактный набор. Самый маленький рабочий пример лежит в
[`tests/test_contract_base_driver.py`](../../tests/test_contract_base_driver.py).

Что `BaseDriver` делает сам, пока метод не переопределён:

| Метод | Умолчание | Переопределить, когда |
|---|---|---|
| `prepare` / `shutdown` | ничего | у SDK есть общий процесс или бинарь, который надо поднять |
| `attach(endpoint)` | `UnsupportedRequirementError` | драйвер подключается к уже запущенным браузерам (провайдер эндпоинтов) |
| `on_disconnect` | события нет - падение найдёт `ping` | SDK сообщает об обрыве сам: пул узнает о падении сразу |
| `kill_browser` | ещё раз `close_browser` | известен процесс браузера: его можно убить |
| `pid` | `None` | браузер - локальный процесс: страж добьёт зависший и подберёт сирот |
| `export_state` | пустое состояние | объявлен `state_support` |
| `add_cookies` | `UnsupportedRequirementError` | драйвер умеет дозаливать куки в живой контекст |
| `capture` | пустые улики | SDK снимает скриншот и разметку |
| `classify` | `None` | у SDK свои ошибки: прокси, упавший браузер, закрытая вкладка |

:::{note}
Наследовать `BaseDriver` необязательно: пул принимает любой объект со всеми методами протокола
`browser_pool.driver.Driver`.
:::

## Проверка драйвера при создании пула

`BrowserPool(driver)` сверяет драйвер с объявленными возможностями (`browser_pool.driver.driver_problems`) и бросает
`ConfigError`, в котором перечислены все расхождения сразу:

- нет метода протокола `Driver`;
- объявлен `window_control="runtime"`, а методов `WindowControl` нет;
- у наследника `BaseDriver` объявлен `state_support`, а `export_state` не переопределён;
- пулу дан провайдер эндпоинтов, а `attach` не переопределён.

Обратное ошибкой не считается. Драйвер может уметь больше, чем объявил: необъявленным пул просто не пользуется.

## Возможности

Через `DriverCapabilities` драйвер сообщает пулу, что он умеет. Всё, что не объявлено, пул считает неподдержанным. По
возможностям он планирует работу и по ним же сразу отказывает identity, если драйвер не может дать ей то, о чём она
просила.

- `proxy_scope`: `"context"` - прокси на контексте, много identity в браузере (Playwright, pydoll); `"browser"` -
  прокси задаётся при запуске, браузер принадлежит «владельцу» (так устроены Camoufox и Selenium; их драйверы в
  поставку не входят, но протокол их допускает); `"external"` - прокси у вендора;
- `can_new_context` - без него на каждый новый контекст запускается новый процесс браузера;
- `fingerprint_scope`, `state_support` (`full` / `cookies` / `none`);
- `persistent_dir` - профиль на диске: `launch` получает `LaunchSpec.user_data_dir` (и прокси, если он есть), а
  `new_context` с `reuse_default` отдаёт готовый контекст профиля. Каталог принадлежит приложению: драйвер его не
  создаёт заранее и не удаляет. Замком профиля и копированием образца занимается пул;
- `proxy_auth`, `proxy_schemes`, `proxy_auth_schemes` - какие прокси SDK поднимет и на каких схемах поддерживает
  авторизацию (Chromium, например, не авторизуется на socks). Невыполнимое пул отвергает сразу при аренде, а из
  источника прокси такой не берёт;
- `context_settings` - какие настройки контекста identity (`ContextOptions`) драйвер применяет: `locale`, `timezone`,
  `geolocation`, `viewport`, `user_agent`. Identity с необъявленной настройкой получит `UnsupportedRequirementError`,
  а не контекст без неё. К готовому контексту (`reuse_default`: профиль на диске, профиль вендора) настройки не
  применяются вовсе, и такая identity тоже получит отказ;
- `debug_options` - какие отладочные опции запуска драйвер применяет: `slow_mo`, `keep_background_active`. Если в
  конфиге задана опция, которую драйвер не поддерживает, пул выдаёт предупреждение при старте;
- `thread_affinity` - синхронный SDK: пул даст каждому браузеру свой поток и направит туда все вызовы;
- `window_control` (`runtime` / `launch_only` / `none`), `new_window`, `max_pages_hint`.

`LaunchSpec` и `ContextSpec` несут общие поля (`headless`, `proxy`, `state`, `locale`, `timezone`, `viewport`…) и
`extra` - нативные опции SDK, которые хуки `before_launch` / `before_context` передают насквозь.
`ContextSpec.reuse_default` значит «не создавать контекст, а взять готовый» (профиль антидетекта, драйвер без
контекстов).

Необязательные части: `WindowControl` (окна для отладки: `window_of`, `get_bounds`, `set_bounds`, `screen_area`,
`bring_to_front`) и `PageLabeler` (`label_page` - подпись окна).

## На что опирается пул

Из сигнатур этих требований не видно, но пул на них рассчитывает. Контрактный набор проверяет каждое из них.

- Любой вызов могут отменить посередине, потому что пул ограничивает каждый вызов своим таймаутом. Метод, который
  что-то создаёт (`launch`, `attach`, `new_context`, `new_page`), при отмене и при своём сбое сам убирает созданное:
  у пула нет ссылки на полусозданный браузер.
- `close_page`, `close_context` и `close_browser` не бросают исключений: ни на уже закрытом объекте, ни на умершем
  браузере, ни при повторном вызове. Готовый контекст (`reuse_default`) драйвер не закрывает, потому что пулу он не
  принадлежит.
- `page_usable` синхронный и дешёвый, без обращения к SDK по сети. Вкладку, которую закрыл сайт или которая умерла
  вместе с контекстом, он должен считать непригодной, иначе пул выдаст её следующей аренде.
- `ping` может вернуть `False` или бросить исключение, для пула это одно и то же. `on_disconnect` необязателен и
  может сработать и тогда, когда браузер закрывает сам драйвер.
- `prepare` и `shutdown` вызываются на каждый пул, а не один раз на процесс. Драйвер считает пулы и освобождает общие
  ресурсы после последнего `shutdown`.
- `export_state` возвращает те `extras`, с которыми контекст был создан (`spec.state.extras`). Пул заменяет запись
  identity снятым состоянием целиком, так что потерянные `extras` означают потерянные токены SDK сайта. Куку, которую
  модель `Cookie` не может представить (без имени), драйвер пропускает; снятие состояния из-за неё не падает.
- `classify` различает, что именно сломалось. Ошибка операции на умершем браузере получает вид `browser` (карантин),
  на закрытой вкладке - `page`, недоступный прокси и отвергнутые им креды - `proxy` (пул сменит прокси). На ошибку,
  которая к драйверу не относится, он отвечает `None`.
- Креды прокси должны доходить до SDK без искажений, в том числе с `@`, `:`, `/`, `%` и пробелами. `Proxy.url`
  кодирует их для URL, и не всякий SDK раскодирует обратно, поэтому надёжнее передавать `proxy.server`,
  `proxy.username`, `proxy.password` раздельно.
- `capture` не бросает исключений и возвращает то, что успел снять.
- `attach` не убивает чужой браузер: `close_browser` для него закрывает только соединение. На неподдержанный
  `endpoint.kind` драйвер отвечает `UnsupportedRequirementError`.
- Секреты (креды прокси, токены в `Endpoint.url`) не попадают в логи и сообщения ошибок.
- SDK импортируется лениво, внутри функций, чтобы ядро и соседние драйверы от него не зависели.

## Синхронный SDK

:::{warning}
Экспериментально: в поставке нет драйвера с `thread_affinity`, поведение проверено только на `FakeDriver`.
:::

С `thread_affinity=True` методы драйвера остаются `async def`, но внутри них стоят блокирующие вызовы SDK. Пул
исполняет их в собственном цикле событий потока браузера, и цикл приложения не блокируется. Синхронные методы (`pid`,
`page_usable`, `on_disconnect`, `classify`) пул зовёт из своего потока, поэтому отвечать на них нужно по состоянию
самого драйвера, не обращаясь к SDK.

## Контрактные тесты

`browser_pool.testing.contract.DriverContractSuite` - один набор тестов для любого драйвера. Сначала он сверяет
возможности с реализацией, потом проверяет всё из раздела «На что опирается пул»:

- изоляцию кук;
- перенос состояния (куки со всеми атрибутами, localStorage, `extras`);
- прокси: на каждой вкладке, с любым паролем, с отвергнутыми кредами и недоступный;
- вид сбоя умершего браузера и отсутствие процесса после закрытия;
- вкладку закрытого контекста, настройки контекста, окна.

Проверки того, что драйвер не объявил, пропускаются.

```python
from browser_pool.testing.contract import DriverContractSuite


class TestMyDriver(DriverContractSuite[Browser, Context, Page]):
    def make_driver(self):
        return MyDriver()

    async def visit(self, page, url):          # открыть адрес средствами SDK и вернуть текст страницы
        await page.goto(url)
        return await page.text()

    async def evaluate(self, page, expression):  # необязательно: JS на странице для localStorage и эмуляции
        return await page.evaluate(expression)
```

:::{important}
Нужен pytest-asyncio. Его режим (strict или auto) не важен: тесты набора помечены сами.
:::

Переопределяются:

- `make_driver` и `visit` - обязательны;
- `evaluate` - без него тесты, которым нужен JS, пропускаются;
- `crash` - как уронить браузер «самого по себе»; по умолчанию набор убивает процесс;
- `launch_spec`, `process_alive`;
- `has_process` - `False` у драйвера без процессов браузера.

Примеры: [`tests/test_contract_playwright.py`](../../tests/test_contract_playwright.py),
[`tests/test_contract_pydoll.py`](../../tests/test_contract_pydoll.py).

Набор запускает браузер сам (`launch`). Подключение (`attach`) и синхронные драйверы (`thread_affinity`) он не
проверяет.

## Откуда браузер: провайдер

Если браузер запускает не драйвер (удалённый CDP, антидетект), драйвер менять не нужно. Нужен провайдер,
`browser_pool.provider.EndpointProvider`, с методами `start` (возвращает `Endpoint`), `stop` и `reap_orphans`. Пул
зовёт `provider.start()`, затем `driver.attach(endpoint)`, и ровно один раз на эндпоинт зовёт `stop`. В поставке есть
`browser_pool.providers.remote_cdp.RemoteCDP` и `browser_pool.providers.adspower.AdsPowerProvider`. Провайдер профилей
(`ProfileProvider`) меняет возможности пары «драйвер и провайдер» методом `adapt(capabilities)`.

## Стабильность

До 1.0 протокол `Driver` может меняться в минорных версиях. Каждое изменение описано в
[`CHANGELOG.md`](../../CHANGELOG.md) вместе с тем, что поправить в драйвере. Новые необязательные методы появляются в
`BaseDriver` с реализацией по умолчанию, так что его наследники продолжают работать. С 1.0 протокол стабилен, ломающие
изменения возможны только в мажорных версиях. Чтобы убедиться, что драйвер по-прежнему соблюдает контракт, прогоняйте
`DriverContractSuite` на каждой новой версии пула.

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`link` Провайдер браузеров
:link: provider
:link-type: doc

Как подключить браузер, который запускает не пул.
:::

:::{grid-item-card} {octicon}`code` Справочник: драйвер
:link: ../reference/api/driver
:link-type: doc

Протокол `Driver`, `BaseDriver`, `DriverCapabilities` и драйверы из поставки.
:::
::::
