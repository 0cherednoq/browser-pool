"""Мелочи листьев (M8.12): гео-проверка, замки, замер памяти, метрики нескольких пулов, псевдонимы типов."""

from __future__ import annotations

import errno
import importlib
import json
import pkgutil
from types import SimpleNamespace
from typing import Any

import pytest

import browser_pool
from browser_pool import BrowserPool
from browser_pool.locks import FileLock
from browser_pool.proxies import HttpGeoChecker, Proxy
from browser_pool.state import SessionState
from browser_pool.testing import FakeDriver

PROXY = Proxy(host="1.2.3.4", port=8080, username="ada", password="S3CRET")


class Fetcher:
    """Подменный `fetch`: отвечает по очереди, считает вызовы."""

    def __init__(self, *answers: bytes | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, url: str, proxy: Proxy, seconds: float) -> bytes:
        _ = url, proxy, seconds
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


GOOD = json.dumps({"status": "success", "query": "5.6.7.8", "countryCode": "DE"}).encode()
FAIL = json.dumps({"status": "fail"}).encode()


# --- гео ---------------------------------------------------------------------------------


async def test_fetch_failure_is_none_and_cached_briefly() -> None:
    fetch = Fetcher(OSError("прокси не отвечает"))
    checker = HttpGeoChecker(fetch=fetch, failure_ttl=60.0)

    assert await checker.check(PROXY) is None
    assert (
        await checker.check(PROXY) is None
    )  # из короткого кэша: мёртвый прокси не платит таймаут каждый раз
    assert fetch.calls == 1

    import asyncio

    await asyncio.sleep(61)
    assert await checker.check(PROXY) is None
    assert fetch.calls == 2


async def test_failed_status_is_not_cached_as_long_as_a_success() -> None:
    fetch = Fetcher(FAIL, GOOD)
    checker = HttpGeoChecker(fetch=fetch, ttl=3600.0, failure_ttl=30.0)

    assert await checker.check(PROXY) is None
    import asyncio

    await asyncio.sleep(31)
    geo = await checker.check(PROXY)

    assert geo is not None
    assert geo.country == "DE"
    assert fetch.calls == 2


async def test_garbage_answer_is_none() -> None:
    checker = HttpGeoChecker(fetch=Fetcher(b"<html>", b'{"status": "success"}', b"[]"))

    assert await checker.check(PROXY) is None
    assert await checker.check(Proxy(host="1.2.3.5", port=80)) is None
    assert await checker.check(Proxy(host="1.2.3.6", port=80)) is None


async def test_cache_has_a_bound() -> None:
    fetch = Fetcher(GOOD)
    checker = HttpGeoChecker(fetch=fetch, max_entries=2)
    proxies = [Proxy(host=f"10.0.0.{n}", port=80) for n in range(3)]

    for proxy in proxies:
        await checker.check(proxy)
    await checker.check(proxies[2])  # в кэше
    await checker.check(proxies[0])  # вытеснен — запрос снова

    assert fetch.calls == 4


# --- замки на файловых системах без блокировок ------------------------------------------------


async def test_filesystem_without_locks_is_an_error_not_endless_waiting(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(fd: int) -> None:
        _ = fd
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr("browser_pool.locks._lock", unsupported)
    lock = FileLock(tmp_path / "x.lock", poll_interval=0.01)

    with pytest.raises(OSError, match="блокировки") as caught:
        await lock.acquire()

    assert "x.lock" in str(caught.value)
    assert not lock.held


async def test_busy_lock_is_still_waiting(tmp_path: Any) -> None:
    import asyncio

    first, second = (
        FileLock(tmp_path / "x.lock", poll_interval=0.01),
        FileLock(tmp_path / "x.lock", poll_interval=0.01),
    )
    await first.acquire()

    waiting = asyncio.ensure_future(second.acquire())
    await asyncio.sleep(0.1)
    assert not waiting.done()
    first.release()
    await waiting
    second.release()


# --- замер памяти дерева ---------------------------------------------------------------------


class FakeProcess:
    """Процесс psutil: RSS считает общую память многократно, PSS — по доле."""

    def __init__(
        self, rss: int, pss: int | None, children: list[FakeProcess] | None = None
    ) -> None:
        self._rss, self._pss, self._children = rss, pss, children or []

    def children(self, recursive: bool = True) -> list[FakeProcess]:
        _ = recursive
        return self._children

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=self._rss)

    def memory_full_info(self) -> SimpleNamespace:
        if self._pss is None:
            raise PermissionError
        return SimpleNamespace(pss=self._pss, uss=self._pss)


def probe_over(root: FakeProcess) -> Any:
    from browser_pool.monitors.psutil import PsutilProbe

    def process(pid: int) -> FakeProcess:
        _ = pid
        return root

    probe = object.__new__(PsutilProbe)
    fake: Any = SimpleNamespace(Process=process, Error=OSError)
    probe._psutil = fake  # pyright: ignore[reportPrivateUsage]
    return probe


def test_tree_memory_counts_shared_pages_once() -> None:
    mb = 1024 * 1024
    renderer = FakeProcess(rss=400 * mb, pss=120 * mb)  # RSS общий с родителем многократно
    gpu = FakeProcess(rss=300 * mb, pss=100 * mb)
    root = FakeProcess(rss=500 * mb, pss=200 * mb, children=[renderer, gpu])

    measured = probe_over(root).tree_rss_mb(1)

    assert measured == pytest.approx(420.0)  # PSS, а не 1200 МБ суммы RSS


def test_tree_memory_falls_back_to_rss_when_full_info_is_denied() -> None:
    mb = 1024 * 1024
    root = FakeProcess(rss=500 * mb, pss=None, children=[FakeProcess(rss=100 * mb, pss=None)])

    assert probe_over(root).tree_rss_mb(1) == pytest.approx(600.0)


# --- несколько пулов в метриках --------------------------------------------------------------


async def test_two_pools_do_not_overwrite_each_others_gauges(fake_driver: FakeDriver) -> None:
    pytest.importorskip("prometheus_client")
    import asyncio

    from prometheus_client import CollectorRegistry

    from browser_pool import Identity
    from browser_pool.monitors.prometheus import PrometheusMetrics

    registry = CollectorRegistry()
    metrics = PrometheusMetrics(registry=registry)
    mail = BrowserPool(fake_driver)
    shop = BrowserPool(FakeDriver())
    metrics.attach(mail, name="mail")
    metrics.attach(shop, name="shop")
    with pytest.raises(ValueError, match="name"):
        metrics.attach(BrowserPool(FakeDriver()), name="mail")

    async with mail, shop:
        async with mail.page(Identity(key="m:1")):
            pass
        await asyncio.sleep(16)  # проверка здоровья → PoolHealth у обоих

    def value(name: str, **labels: str) -> float | None:
        return registry.get_sample_value(f"browser_pool_{name}", labels)

    assert value("leases_total", pool="mail", outcome="ok") == 1
    assert value("leases_total", pool="shop", outcome="ok") is None
    assert value("capacity", pool="mail", scope="total") is not None
    assert value("capacity", pool="shop", scope="total") is not None


# --- разбор сессии ---------------------------------------------------------------------------


def test_header_cookies_secure_flag_is_a_choice() -> None:
    secure = SessionState.parse("sid=abc", default_domain=".example.com")
    plain = SessionState.parse("sid=abc", default_domain=".example.com", secure=False)

    assert secure.cookies[0].secure
    assert not plain.cookies[0].secure


# --- псевдонимы типов ------------------------------------------------------------------------


def exported_aliases() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for info in pkgutil.walk_packages(browser_pool.__path__, "browser_pool."):
        if any(part.startswith("_") for part in info.name.split(".")):
            continue
        if info.name.endswith(("drivers.playwright", "drivers.pydoll", "monitors.prometheus")):
            continue
        module = importlib.import_module(info.name)
        found.extend(
            (info.name, name)
            for name in getattr(module, "__all__", ())
            if type(getattr(module, name, None)).__name__ == "TypeAliasType"
        )
    return found


@pytest.mark.parametrize(("module", "name"), exported_aliases())
def test_exported_type_aliases_can_be_evaluated(module: str, name: str) -> None:
    alias = getattr(importlib.import_module(module), name)

    assert (
        alias.__value__ is not None
    )  # NameError, если имя из значения импортировано под TYPE_CHECKING
