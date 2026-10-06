# Прокси

Постоянный адрес у аккаунта и мёртвые прокси на паузе.

Продавец присылает список прокси, где половина строк записана как `host:port:user:pass`, а половина как
`user:pass@host:port`, и под него нужен парсер. Аккаунт сегодня заходит с немецкого адреса, а завтра с бразильского,
и сайт просит подтвердить вход. Умерший прокси продолжает раздаваться новым задачам, и все они падают по таймауту. А
контекст с немецким прокси, часовым поясом Киева и локалью en-US сайт замечает с первого запроса.

## Список прокси и закрепление за аккаунтом

Список прокси отдаётся пулу, а правило выбора задаётся у аккаунта.

```python
from pathlib import Path

from browser_pool import BrowserPool, Identity, ProxyPolicy
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.proxies import ProxyList

proxies = ProxyList.parse_lines(Path("proxies.txt").read_text().splitlines())

accounts = [
    Identity(key=f"mail:{login}", payload=login, proxy=ProxyPolicy.sticky())  # свой прокси навсегда
    for login in ("anna", "boris", "vera")
]

pool = BrowserPool(PlaywrightDriver(), proxy_source=proxies)
```

Аккаунт получает один и тот же прокси, в том числе после перезапуска процесса. Если прокси убрать из списка,
переедут только его аккаунты, остальные останутся на своих.

Прокси задаётся на контексте, поэтому аккаунты в одном браузере ходят через разные прокси. Если прокси требует
авторизацию, логин и пароль подставляет драйвер.

Формат строки определяется автоматически:

| Запись | Пример |
|---|---|
| `host:port` | `10.0.0.9:8000` |
| `scheme://user:pass@host:port` | `socks5://anna:secret@10.0.0.9:1080` |
| `user:pass@host:port` | `anna:secret@10.0.0.9:8000` |
| `host:port@user:pass` | `10.0.0.9:8000@anna:secret` |
| `host:port:user:pass` | `10.0.0.9:8000:anna:secret` |
| `user:pass:host:port` | `anna:secret:10.0.0.9:8000` |

Схемы: http, https, socks4, socks5.

:::{note}
Строка, которую нельзя разобрать однозначно, отвергается. Пароль в текст ошибки не попадает.
:::

## Как выбрать прокси для аккаунта

| `ProxyPolicy` | Что значит |
|---|---|
| `ProxyPolicy.pool()` (по умолчанию) | любой пригодный прокси из источника, на каждый новый контекст |
| `ProxyPolicy.sticky()` | один и тот же прокси для аккаунта, и после перезапуска |
| `ProxyPolicy.fixed(proxy)` | ровно этот прокси, например записанный за аккаунтом в вашей БД |
| `ProxyPolicy.direct()` | без прокси |
| `ProxyPolicy.external()` | прокси задаёт антидетект-браузер, пул не вмешивается |

Как сам список раздаёт прокси, задаёт `ProxyList(strategy=...)`:

- `round_robin` выдаёт прокси по кругу;
- `least_used` выдаёт тот, на котором меньше живых аккаунтов;
- `sticky` ведёт каждый аккаунт к своему прокси.

## Мёртвый прокси уходит сам

Когда задача падает из-за прокси (драйвер распознаёт сетевые ошибки и отказ авторизации прокси), пул сообщает
источнику, что прокси не прошёл, и переоткрывает контекст аккаунта с другим прокси, до `Recovery.proxy_retries` раз
(по умолчанию 2).

`ProxyList` после трёх сбоев подряд (`breaker_failures`) или одного бана ставит прокси на паузу на 10 минут
(`breaker_cooldown`). Каждая следующая пауза подряд вдвое дольше, а успех обнуляет счёт.

```python
from browser_pool.proxies import Proxy

proxies.status()  # здоровье каждого прокси: сбои, пауза, сколько аккаунтов на нём
await proxies.ban("de-1")  # сайт его забанил: сразу на паузу
proxies.add(Proxy.parse("user:pass@10.0.0.9:8000", id="de-9"))  # добавить на лету
proxies.remove("de-2")  # убрать: его аккаунты дорабатывают, новые его не получат
```

Число аккаунтов на одном прокси ограничивает `Limits.max_identities_per_proxy` (или `max_identities_per_proxy` у
самого списка).

## Часовой пояс и локаль по стране прокси

```python
from browser_pool import BrowserPool
from browser_pool.drivers.playwright import PlaywrightDriver
from browser_pool.proxies import HttpGeoChecker

pool = BrowserPool(PlaywrightDriver(), proxy_source=proxies, proxy_checker=HttpGeoChecker())
```

Пул узнаёт, в какой стране выходит прокси, и подставляет в контекст `timezone`, `locale` и `geolocation`, но
только те, что аккаунт не задал сам. Ответы кэшируются на час, неудачи - на минуту.

:::{warning}
По умолчанию запрос идёт к ip-api.com, а он бесплатен только для некоммерческого использования. Для коммерческой
работы передайте через `url` свой сервис с таким же форматом ответа.
:::

## Дешёвые прокси, пока сайт пускает

Датацентровые прокси дешёвые, но сайты чаще им отказывают; резидентные надёжнее, но дороже. `TieredProxyList` держит
аккаунты на дешёвом уровне, пока он работает, и переводит на дорогой, когда начинаются сбои.

```python
from pathlib import Path

from browser_pool.proxies import Proxy, TieredProxyList

datacenter = [Proxy.parse(line) for line in Path("dc.txt").read_text().splitlines()]
residential = [Proxy.parse(line) for line in Path("resi.txt").read_text().splitlines()]

proxies = TieredProxyList([datacenter, residential], raise_at=0.3, lower_after=50)
```

Уровень ведётся отдельно для каждой группы аккаунтов (метка `labels["service"]`). Когда доля сбоев за последние 10
исходов доходит до 30%, группа переходит на уровень дороже; после 50 успехов подряд она пробует уровень дешевле.
`None` в списке уровня означает выход напрямую: `[[None], datacenter, residential]` начинает вообще без прокси.

## Прокси в вашей БД

Если прокси хранятся в вашей БД, источник собирается из двух функций: одна выдаёт прокси для заявки, другая принимает
отчёт об исходе.

```python
from browser_pool.proxies import CallbackProxySource, Proxy, ProxyRequest


async def pick(request: ProxyRequest) -> Proxy | None:
    row = await db.fetch_proxy_for(request.identity_key)  # db: ваш репозиторий прокси
    return Proxy.parse(row.url, id=str(row.id)) if row else None


async def report(lease, outcome) -> None:
    await db.mark_proxy(lease.proxy_id, outcome.kind)  # ok / failed / banned


source = CallbackProxySource(pick, on_report=report)
```

## HTTP-клиент с того же адреса

Если часть работы удобнее делать HTTP-запросами, а не браузером, возьмите куки и прокси аренды: сайт увидит тот же
адрес и ту же сессию.

```python
async with pool.page(account) as lease:
    cookies = await lease.cookies(domain="mail.example")
    proxy_url = lease.proxy.url if lease.proxy else None
```

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`sync` Сбои и повторы
:link: recovery
:link-type: doc

Как пул узнаёт, что сломался именно прокси, и что он делает с остальными сбоями.
:::

:::{grid-item-card} {octicon}`code` Справочник: прокси
:link: ../reference/api/proxies
:link-type: doc

`Proxy`, `ProxyList`, `TieredProxyList` и протокол источника.
:::
::::
