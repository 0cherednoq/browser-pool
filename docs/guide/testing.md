# Тесты

Браузерная автоматизация проверяется без браузера.

Чтобы проверить, что аккаунт уходит на паузу после трёх неудачных входов, нужны настоящий Chrome, сайт, который
откажет три раза, и десять минут ожидания. Поэтому таких тестов обычно не пишут: повторы, паузы и падения браузера
проверяют руками один раз, когда пишут код, а следующая правка ломает их незаметно.

## Фейковый драйвер и виртуальное время

`FakeDriver` заменяет браузер: пул работает с ним так же, как с настоящим, а тест заказывает нужные сбои. Метка
`virtual_time` проматывает время: `asyncio.sleep(600)` проходит мгновенно, TTL и паузы пула тоже.

```bash
pip install --pre "browser-pool[testing]"
```

Экстра ставит pytest и pytest-asyncio не ниже 1.4. Плагин подключается одной строкой:

```python
# conftest.py
pytest_plugins = ["browser_pool.testing.pytest_plugin"]
```

```python
# test_login.py
import pytest

from browser_pool import BaseFlow, BrowserPool, Identity, OpenRequest
from browser_pool.errors import IdentityCoolingDownError

pytestmark = pytest.mark.asyncio  # при asyncio_mode = "auto" метка не нужна


class AlwaysFailingLoginFlow(BaseFlow):
    async def open(self, ctx: OpenRequest) -> str:
        raise RuntimeError("неверный пароль")


@pytest.mark.virtual_time
async def test_failed_login_puts_account_on_pause(fake_driver):
    account = Identity(key="mail:42")
    async with BrowserPool(fake_driver, flow=AlwaysFailingLoginFlow()) as pool:
        with pytest.raises(RuntimeError):
            async with pool.page(account):
                pass

        assert pool.identity_status(account).cooling_until is not None
        with pytest.raises(IdentityCoolingDownError):
            async with pool.page(account, wait_cooldown=False):
                pass
```

После каждого теста фикстура `fake_driver` проверяет, что ничего не утекло: ни браузеров, ни контекстов, ни вкладок,
ни задач, начатых тестом.

## Заказать сбой

```python
from browser_pool.testing import FakeNetworkError

fake_driver.faults.fail("new_page", FakeNetworkError("сеть"), times=2)  # две следующие вкладки не откроются
fake_driver.faults.hang("launch")  # запуск браузера зависнет: проверить таймаут
fake_driver.faults.delay("new_context", 5.0)  # контекст создаётся 5 секунд
fake_driver.crash(lease.browser)  # браузер «упал» посреди аренды
```

Так проверяются повторы `pool.run`, реакция на падение браузера, паузы после неудачных входов и таймауты. Тесты
занимают миллисекунды и идут в CI.

:::{note}
Виртуальное время стоит, пока в цикле есть настоящий ввод-вывод (сокет, подпроцесс, поток): `asyncio.timeout`
вокруг чтения сети не сработает раньше ответа.
:::

## Свой драйвер

Для адаптера своего браузерного SDK есть `DriverContractSuite` (`browser_pool.testing.contract`) - набор тестов,
который проверяет, что драйвер выполняет требования протокола. Как написать драйвер, описано в
[driver.md](../extending/driver.md).

## Что дальше

::::{grid} 1 1 2 2
:gutter: 2
:padding: 0
:class-row: surface

:::{grid-item-card} {octicon}`sign-in` Как написать SessionFlow
:link: ../extending/flow
:link-type: doc

Вход на сайт и подготовка вкладок, описанные один раз.
:::

:::{grid-item-card} {octicon}`code` Справочник: тесты
:link: ../reference/api/testing
:link-type: doc

`FakeDriver`, заказ сбоев, плагин pytest и `DriverContractSuite`.
:::
::::
