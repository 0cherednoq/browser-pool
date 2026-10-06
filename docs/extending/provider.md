# Провайдер браузеров

Как подключить браузер, который запускает не пул.

Иногда браузер уже запущен: Chrome на соседней машине с открытым портом отладки, облачный Chromium, профиль
антидетекта. Драйвер для такого случая менять не нужно. Откуда берётся браузер, описывает отдельный объект,
провайдер эндпоинтов.

Без провайдера пул вызывает `driver.launch()`. С провайдером он обращается к `provider.start()`, получает адрес и
подключается через `driver.attach(endpoint)`. Драйвер знает SDK, провайдер знает, где взять браузер, и любую пару
можно собрать без отдельного адаптера.

## Готовые провайдеры

Уже запущенный браузер по CDP:

```python
from browser_pool import BrowserPool, PoolConfig, Topology
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.providers import RemoteCDP

pool = BrowserPool(
    PlaywrightDriver(),
    config=PoolConfig(topology=Topology(browsers=1)),  # один адрес на один браузер пула
    provider=RemoteCDP("http://10.0.0.5:9222"),
)
```

:::{important}
`RemoteCDP` принимает один адрес на слот пула, поэтому `Topology.browsers` должно быть не больше числа адресов:
лишний слот получит `NoFreeEndpointError`.
:::

Эти браузеры пул не запускает и не останавливает: `stop` только возвращает адрес в запас. Токен в адресе
(`?token=...`) сохраняется, а в тексты ошибок адрес не попадает.

Профили AdsPower:

```python
from browser_pool import BrowserPool
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.providers import AdsPowerProvider

provider = AdsPowerProvider(
    profile=lambda identity: identity.payload.adspower_id,  # какой профиль открыть для identity
    registry="adspower-profiles.json",  # реестр поднятых профилей, чтобы подобрать их после падения
)
pool = BrowserPool(PlaywrightDriver(), provider=provider)
```

Если `profile` не задан, профиль берётся по ключу identity. Вызовы локального API идут по одному и не чаще
`requests_per_second` (по умолчанию один в секунду).

Браузер антидетекта и есть профиль вендора: отпечаток, прокси и куки принадлежат ему. Поэтому у такой пары браузер
принадлежит одной identity, контекст берётся готовый, а сессию пул не сохраняет и не восстанавливает.

:::{important}
Настройки контекста identity (`Identity.context_options`) к готовому контексту не применяются, и identity, которая
их задала, получит `UnsupportedRequirementError`.
:::

## Свой провайдер

Провайдер - это объект с тремя методами по протоколу `browser_pool.provider.EndpointProvider`:

```python
from browser_pool.driver import Endpoint
from browser_pool.provider import EndpointRequest


class CloudBrowsers:
    """Браузеры облачного сервиса: сессия на слот пула."""

    def __init__(self, client) -> None:
        self.client = client  # клиент API вашего сервиса

    async def start(self, request: EndpointRequest) -> Endpoint:
        session = await self.client.create_session(name=request.browser_id)
        return Endpoint(kind="cdp", url=session.cdp_url)

    async def stop(self, endpoint: Endpoint) -> None:
        await self.client.close_session(endpoint.url)

    async def reap_orphans(self) -> int:
        return await self.client.close_stale_sessions()
```

Правила для методов:

- `start` получает слот пула (`request.browser_id`), спецификацию запуска после хуков `before_launch`
  (`request.spec`) и, если браузер принадлежит одной identity, саму identity (`request.identity`).
- `stop` пул вызывает ровно один раз на каждый выданный эндпоинт, что бы ни случилось с браузером: не подключился,
  закрыт, добит, пул остановлен. Сбой `stop` попадает в лог и исключением не становится.
- `reap_orphans` вызывается при старте пула и освобождает то, что осталось от прошлого запуска. Возвращает число
  освобождённых браузеров.
- Каждый вызов пул ограничивает своим таймаутом.
- Если свободных адресов нет, провайдер бросает `NoFreeEndpointError`.

:::{warning}
`Endpoint.url` часто несёт токен. В логи и сообщения ошибок его выводить нельзя.
:::

Вид эндпоинта (`kind`) говорит драйверу, как подключаться: `cdp`, `playwright_ws` или `webdriver`. Драйвер, который
такой вид не поддерживает, отвечает `UnsupportedRequirementError`.

## Провайдер профилей

Если браузер провайдера и есть профиль вендора, как у антидетектов, провайдер реализует ещё и
`browser_pool.provider.ProfileProvider`: добавляет метод `adapt(capabilities)`. Он возвращает возможности пары
«драйвер и провайдер», и пул планирует работу по ним: прокси у вендора, новых контекстов нет, состояние хранит
вендор.

## Проверка в тестах

`browser_pool.testing.FakeEndpointProvider` заменяет провайдер в тестах без браузера, так же как `FakeDriver`
заменяет драйвер. Подробнее на странице [Тесты](../guide/testing.md).

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`cpu` Устройство пула
:link: ../explanation/architecture
:link-type: doc

Части пула, путь одной аренды и жизнь браузера.
:::

:::{grid-item-card} {octicon}`code` Справочник: провайдеры
:link: ../reference/api/provider
:link-type: doc

`EndpointProvider`, `ProfileProvider`, `EndpointRequest` и готовые провайдеры.
:::
::::
