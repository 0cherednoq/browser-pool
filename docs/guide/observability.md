# Наблюдение

Снимок, события и метрики показывают, что пул делает прямо сейчас.

Скрипт работает уже час, и кажется, что медленно. Чтобы понять почему, нужно знать, сколько браузеров живо, какие
аккаунты на паузе и по какой причине, кто держит вкладки, пока двадцать задач стоят в очереди. Расставлять для этого
`print` и перезапускать скрипт не нужно: пул отдаёт своё состояние снимком, событиями и метриками.

## Снимок

Снимок - это всё состояние пула одним объектом:

```python
snapshot = pool.snapshot()
print(snapshot.oldest_waiter_seconds, snapshot.oldest_waiter_candidates)  # кто ждёт дольше всех и чего
print(snapshot.leases_active, snapshot.waiting)  # сколько вкладок занято, сколько заявок ждёт
for browser in snapshot.browsers:
    print(browser.id, browser.state)  # healthy, draining, quarantined, restarting
for context in snapshot.contexts:
    print(context.key)  # какие аккаунты сейчас открыты
print(snapshot.counters.restarts, snapshot.pressure)  # перезапуски браузеров; что давит на хост
```

:::{note}
В снимке только ключи аккаунтов, номера, состояния и числа. Ни payload, ни прокси, ни кук в нём нет, поэтому его
можно класть в лог и отдавать в админку.
:::

## События

На события подписываются через `pool.on`:

```python
from browser_pool.events import BrowserRestarted, IdentityBlocked, LeaseReleased, OpenFailed

pool.on(IdentityBlocked, lambda event: notify(f"аккаунт {event.key} заблокирован ({event.kind})"))
pool.on(OpenFailed, lambda event: log.warning("вход %s не удался, повтор через %s с", event.key, event.retry_in))
pool.on(BrowserRestarted, lambda event: log.info("браузер %s перезапущен", event.browser_id))
pool.on(LeaseReleased, lambda event: stats.observe(event.key, event.held, event.outcome))
```

Событий почти три десятка (`browser_pool.events`):

- жизнь браузеров и контекстов;
- входы и сохранения сессий;
- паузы и блокировки аккаунтов;
- сбои прокси;
- повторы задач;
- давление на хост;
- подозрение на утечку аренды;
- размещение окон.

`PoolHealth` приходит после каждой проверки здоровья, его удобно выводить на дашборд. `pool.on` возвращает функцию
отписки.

## Метрики Prometheus

```python
from browser_pool.monitors.prometheus import PrometheusMetrics

PrometheusMetrics().attach(pool)
```

:::{important}
Метрикам нужна экстра `browser-pool[prometheus]`.
:::

## Зависшее ожидание

Когда заявка ждёт вкладку дольше минуты (`Timeouts.acquire_watchdog`), снимок пула уходит в лог и в событие
`AcquireWatchdog`, а ожидание продолжается. По снимку видно, кто держит вкладки.

## События и хуки

События только сообщают о том, что произошло. Чтобы изменить работу пула (дописать аргументы запуска, подключить
stealth, блокировать картинки), есть хуки:

```python
@pool.hooks.after_context_created
async def block_images(context, identity) -> None:
    await context.route("**/*.{png,jpg,jpeg,webp}", lambda route: route.abort())
```

Точки: `before_launch`, `after_browser_started`, `before_context`, `after_context_created`, `after_page_created`,
`after_acquire`, `before_release`. Набор хуков можно оформить плагином - объектом с этими методами,
`BrowserPool(plugins=[...])`.

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`beaker` Тесты
:link: testing
:link-type: doc

Фейковый драйвер и виртуальное время вместо настоящего браузера.
:::

:::{grid-item-card} {octicon}`code` Справочник: события
:link: ../reference/events
:link-type: doc

Все события пула по группам, с полями каждого.
:::
::::
