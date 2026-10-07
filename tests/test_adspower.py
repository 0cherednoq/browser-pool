"""`AdsPowerProvider`: профиль антидетекта арендуется как identity.

AdsPower здесь фейковый — транспорт, отвечающий как локальный API v1, или HTTP-сервер с тем же
API, который поднимает настоящий Chrome: так проверяется и сам провайдер, и то, что пул работает
в готовом контексте профиля (`contexts[0]`), а не в новом.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast, override

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig, ProxyPolicy
from browser_pool.config import Lifecycle, Limits, Recycling, Timeouts, Topology
from browser_pool.driver import LaunchSpec
from browser_pool.events import OrphansReaped
from browser_pool.procguard import process_token
from browser_pool.provider import EndpointRequest, ProfileProvider
from browser_pool.providers.adspower import AdsPowerError, AdsPowerProvider
from browser_pool.state import Cookie, MemoryStateStore
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage
from browser_pool.testing.fake_driver import FAKE_CAPABILITIES

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

A = Identity(key="ads:k1", proxy=ProxyPolicy.external())
B = Identity(key="ads:k2", proxy=ProxyPolicy.external())


def profile_id(identity: Identity) -> str:
    return identity.key.removeprefix("ads:")


@dataclass
class Call:
    at: float
    path: str
    params: dict[str, str]
    headers: dict[str, str]


@dataclass
class FakeAdsPower:
    """Транспорт, отвечающий как локальный API AdsPower v1."""

    active: set[str] = field(default_factory=set[str])
    calls: list[Call] = field(default_factory=list[Call])
    errors: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])
    """Путь → (code, msg) один раз."""

    async def __call__(self, url: str, headers: Mapping[str, str], seconds: float) -> bytes:
        _ = seconds
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append(
            Call(asyncio.get_running_loop().time(), parts.path, params, dict(headers))
        )
        await asyncio.sleep(0)
        if parts.path in self.errors:
            code, message = self.errors.pop(parts.path)
            return json.dumps({"code": code, "msg": message}).encode()
        user_id = params["user_id"]
        data: dict[str, Any] = {}
        if parts.path.endswith("/start"):
            self.active.add(user_id)
            data = {
                "ws": {"puppeteer": f"ws://127.0.0.1:1/devtools/browser/{user_id}"},
                "webdriver": "C:/adspower/chromedriver.exe",
            }
        elif parts.path.endswith("/stop"):
            self.active.discard(user_id)
        else:
            data = {"status": "Active" if user_id in self.active else "Inactive"}
        return json.dumps({"code": 0, "msg": "success", "data": data}).encode()

    def paths(self) -> list[str]:
        return [f"{call.path.rsplit('/', 1)[1]}:{call.params['user_id']}" for call in self.calls]


def make_pool(driver: FakeDriver, provider: AdsPowerProvider, **options: Any) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1, pages_per_browser=4),
            limits=Limits(spawn_delay=0.0),
            lifecycle=Lifecycle(healthcheck_interval=30.0),
            recycling=Recycling(recycle_jitter=0.0),
            timeouts=Timeouts(close=5.0, startup=10.0),
        ),
        provider=provider,
        **options,
    )


def adspower(api: FakeAdsPower, **options: Any) -> AdsPowerProvider:
    return AdsPowerProvider(profile=profile_id, api_key="secret-key", transport=api, **options)


# --- возможности ---------------------------------------------------------------------------


def test_adspower_turns_any_driver_into_a_profile_driver() -> None:
    provider = AdsPowerProvider(api_key="secret-key")
    capabilities = provider.adapt(FAKE_CAPABILITIES)

    assert isinstance(provider, ProfileProvider)
    assert capabilities.proxy_scope == "external"
    assert not capabilities.can_new_context
    assert capabilities.state_support == "none"
    assert "secret-key" not in repr(provider)


# --- пул на профилях ------------------------------------------------------------------------


async def test_profile_is_leased_in_its_own_context(fake_driver: FakeDriver) -> None:
    api = FakeAdsPower()
    store = MemoryStateStore()

    async with make_pool(fake_driver, adspower(api), state_store=store) as pool:
        async with pool.page(A) as lease:
            assert lease.context.default  # готовый контекст профиля, не новый
            assert lease.proxy is None  # прокси у вендора
            assert lease.browser.endpoint is not None
            assert lease.browser.endpoint.url.endswith("/k1")
            await fake_driver.add_cookies(
                lease.context, [Cookie(name="sid", value="1", domain="x")]
            )
        assert api.active == {"k1"}

    assert api.paths() == ["start:k1", "stop:k1"]
    start = api.calls[0]
    assert start.params["headless"] == "1"
    assert start.params["open_tabs"] == "1"
    assert start.headers == {"Authorization": "Bearer secret-key"}
    assert api.active == set()
    assert await store.load(A.key) is None  # состояние хранит AdsPower, не пул


async def test_profiles_share_a_slot_one_after_another(fake_driver: FakeDriver) -> None:
    api = FakeAdsPower()

    async with make_pool(fake_driver, adspower(api)) as pool:
        async with pool.page(A):
            pass
        async with pool.page(B):
            pass

    assert api.paths() == ["start:k1", "stop:k1", "start:k2", "stop:k2"]
    moments = [call.at for call in api.calls]
    assert all(later - earlier >= 1.0 for earlier, later in itertools.pairwise(moments))


async def test_api_calls_go_one_at_a_time_within_the_rate(fake_driver: FakeDriver) -> None:
    api = FakeAdsPower()
    provider = adspower(api, requests_per_second=2.0)
    pool = BrowserPool(
        fake_driver,
        config=PoolConfig(topology=Topology(browsers=3), limits=Limits(spawn_delay=0.0)),
        provider=provider,
    )
    identities = [Identity(key=f"ads:k{index}", proxy=ProxyPolicy.external()) for index in range(3)]

    async with pool:
        await asyncio.gather(*(_use(pool, identity) for identity in identities))

    moments = [call.at for call in api.calls]
    assert len(moments) == 6
    assert all(later - earlier >= 0.5 for earlier, later in itertools.pairwise(moments))


async def _use(pool: Pool, identity: Identity) -> None:
    async with pool.page(identity):
        await asyncio.sleep(0.1)


async def test_api_error_fails_the_lease_and_leaks_nothing(fake_driver: FakeDriver) -> None:
    api = FakeAdsPower(errors={"/api/v1/browser/start": (-1, "profile does not exist")})

    async with make_pool(fake_driver, adspower(api)) as pool:
        with pytest.raises(AdsPowerError, match="profile does not exist"):
            async with pool.page(A):
                pass

    assert api.paths() == ["start:k1"]  # стоп не нужен: профиль не поднялся
    assert api.active == set()


async def test_orphans_from_the_registry_are_closed(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    registry = tmp_path / "adspower.json"
    registry.write_text(json.dumps(["k1", "k2"]), encoding="utf-8")
    api = FakeAdsPower(active={"k1", "stranger"})
    reaped: list[int] = []
    pool = make_pool(fake_driver, adspower(api, registry=registry))
    pool.on(OrphansReaped, lambda event: reaped.append(event.count))

    async with pool:
        await asyncio.sleep(0)

    assert reaped == [1]
    assert api.active == {"stranger"}  # чужой профиль не тронут
    assert json.loads(registry.read_text(encoding="utf-8")) == {}


async def test_profile_that_did_not_stop_stays_in_the_registry(
    fake_driver: FakeDriver, tmp_path: Path
) -> None:
    registry = tmp_path / "adspower.json"
    api = FakeAdsPower(errors={"/api/v1/browser/stop": (-1, "busy")})

    async with make_pool(fake_driver, adspower(api, registry=registry)) as pool:
        async with pool.page(A):
            assert set(json.loads(registry.read_text(encoding="utf-8"))) == {"k1"}
        await pool.stop()
        assert pool.snapshot().counters.close_failures == 1

    assert set(json.loads(registry.read_text(encoding="utf-8"))) == {"k1"}


# --- настоящий HTTP -----------------------------------------------------------------------


@contextlib.contextmanager
def adspower_server(
    start: Callable[[str], str], stop: Callable[[str], None] | None = None
) -> Generator[tuple[str, list[tuple[str, str | None]]]]:
    """HTTP-сервер с API AdsPower v1: `start(user_id) -> ws`, `stop(user_id)`."""
    seen: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            params = dict(urllib.parse.parse_qsl(parts.query))
            seen.append((parts.path, self.headers.get("Authorization")))
            data: dict[str, Any] = {}
            if parts.path == "/api/v1/browser/start":
                data = {"ws": {"puppeteer": start(params["user_id"])}}
            elif parts.path == "/api/v1/browser/stop" and stop is not None:
                stop(params["user_id"])
            body = json.dumps({"code": 0, "msg": "success", "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: Any) -> None:
            _ = format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


async def test_default_transport_speaks_http_with_the_key(fake_driver: FakeDriver) -> None:
    with adspower_server(lambda user_id: f"ws://127.0.0.1:1/devtools/browser/{user_id}") as (
        api,
        seen,
    ):
        provider = AdsPowerProvider(api=api, api_key="secret-key", profile=profile_id)
        async with make_pool(fake_driver, provider) as pool, pool.page(A) as lease:
            assert lease.browser.endpoint is not None

    assert seen == [
        ("/api/v1/browser/start", "Bearer secret-key"),
        ("/api/v1/browser/stop", "Bearer secret-key"),
    ]


# --- настоящий Chrome за фейковым AdsPower --------------------------------------------------

CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)


class ChromeProfiles:
    """«AdsPower» на настоящем Chrome: профиль — свой каталог и свой процесс с портом отладки."""

    def __init__(self, binary: str, root: Path) -> None:
        self.binary = binary
        self.root = root
        self.processes: dict[str, subprocess.Popen[bytes]] = {}

    def start(self, user_id: str) -> str:
        port = _free_port()
        log = self.root / f"{user_id}.stderr"
        with log.open("wb") as stderr:  # процесс держит свою копию дескриптора
            process = subprocess.Popen(
                [
                    self.binary,
                    "--headless=new",
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={self.root / user_id}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        self.processes[user_id] = process
        # Первый запуск Chrome на машине CI холодный; предел — меньше `Timeouts.startup` пула.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and process.poll() is None:
            with contextlib.suppress(OSError):
                address = f"http://127.0.0.1:{port}/json/version"
                with urllib.request.urlopen(address, timeout=1) as response:
                    return cast("str", json.loads(response.read())["webSocketDebuggerUrl"])
            time.sleep(0.1)
        errors = log.read_text("utf-8", "replace")[-2000:]
        msg = f"Chrome профиля не открыл порт отладки (код выхода {process.poll()}): {errors}"
        raise RuntimeError(msg)

    def stop(self, user_id: str) -> None:
        process = self.processes.pop(user_id)
        process.kill()
        process.wait(timeout=10)

    def close(self) -> None:
        for user_id in tuple(self.processes):
            self.stop(user_id)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def profiles() -> Generator[ChromeProfiles]:
    binary = next((path for path in CHROME_PATHS if Path(path).exists()), None) or shutil.which(
        "chrome"
    )
    if binary is None:
        pytest.skip("нет Chrome")
    with tempfile.TemporaryDirectory() as root:
        chrome = ChromeProfiles(binary, Path(root))
        try:
            yield chrome
        finally:
            chrome.close()


def browser_config() -> PoolConfig:
    return PoolConfig(topology=Topology(browsers=1), limits=Limits(spawn_delay=0.0))


@pytest.mark.browser
async def test_playwright_works_in_the_profile_context(profiles: ChromeProfiles) -> None:
    pytest.importorskip("playwright.async_api")
    from browser_pool.drivers.playwright import PlaywrightDriver

    with adspower_server(profiles.start, profiles.stop) as (api, _seen):
        provider = AdsPowerProvider(api=api, profile=profile_id, requests_per_second=10.0)
        pool = BrowserPool(PlaywrightDriver(), config=browser_config(), provider=provider)
        async with pool:
            async with pool.page(A) as lease:
                assert lease.context is lease.browser.contexts[0]
                await lease.page.set_content("<h1>профиль</h1>")
                assert await lease.page.inner_text("h1") == "профиль"
            async with pool.page(A) as lease:  # тот же профиль — снова его контекст
                assert lease.context is lease.browser.contexts[0]

    assert profiles.processes == {}  # профиль остановлен через API


@pytest.mark.browser
@pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction':DeprecationWarning")
async def test_pydoll_works_in_the_profile_context(profiles: ChromeProfiles) -> None:
    pytest.importorskip("pydoll")
    from browser_pool.drivers.pydoll import PydollDriver

    with adspower_server(profiles.start, profiles.stop) as (api, _seen):
        provider = AdsPowerProvider(api=api, profile=profile_id, requests_per_second=10.0)
        pool = BrowserPool(PydollDriver(), config=browser_config(), provider=provider)
        async with pool, pool.page(A) as lease:
            assert lease.context.id is None  # готовый контекст профиля
            response = await lease.page.execute_script("2 + 2", return_by_value=True)
            assert cast("dict[str, Any]", response)["result"]["result"]["value"] == 4

    assert profiles.processes == {}


# --- M8.11: отмена старта, сироты чужого процесса, ошибки транспорта ------------------------


class StartThenHang(FakeAdsPower):
    """Профиль поднялся, а ответ потерян: запрос старта не возвращается."""

    @override
    async def __call__(self, url: str, headers: Mapping[str, str], seconds: float) -> bytes:
        answer = await super().__call__(url, headers, seconds)
        if urllib.parse.urlsplit(url).path.endswith("/start"):
            await asyncio.sleep(3600)
        return answer


def request_for(identity: Identity) -> EndpointRequest:
    return EndpointRequest(browser_id="browser-0", identity=identity, spec=LaunchSpec())


async def test_profile_started_before_a_cancelled_start_is_closed(tmp_path: Path) -> None:
    api = StartThenHang()
    registry = tmp_path / "adspower.json"
    provider = adspower(api, registry=registry)

    task = asyncio.ensure_future(provider.start(request_for(A)))
    await asyncio.sleep(1)
    assert api.active == {"k1"}  # на стороне AdsPower профиль уже открыт
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(5)  # уборка идёт отдельной задачей провайдера

    assert api.active == set()
    assert json.loads(registry.read_text(encoding="utf-8")) == {}


async def test_start_without_a_cdp_address_closes_the_profile() -> None:
    class NoWebSocket(FakeAdsPower):
        @override
        async def __call__(self, url: str, headers: Mapping[str, str], seconds: float) -> bytes:
            answer = json.loads(await super().__call__(url, headers, seconds))
            if urllib.parse.urlsplit(url).path.endswith("/start"):
                answer["data"] = {}
            return json.dumps(answer).encode()

    api = NoWebSocket()
    provider = adspower(api)

    with pytest.raises(AdsPowerError, match="адрес CDP"):
        await provider.start(request_for(A))

    assert api.active == set()


async def test_concurrent_starts_all_land_in_the_registry(tmp_path: Path) -> None:
    api = FakeAdsPower()
    registry = tmp_path / "adspower.json"
    provider = adspower(api, registry=registry, requests_per_second=1000.0)
    identities = [Identity(key=f"ads:k{n}", proxy=ProxyPolicy.external()) for n in range(8)]

    await asyncio.gather(*(provider.start(request_for(identity)) for identity in identities))

    assert set(json.loads(registry.read_text(encoding="utf-8"))) == {f"k{n}" for n in range(8)}


def owner_of_this_process() -> dict[str, Any]:
    return {"pid": os.getpid(), "token": process_token(os.getpid())}


async def test_reap_leaves_profiles_of_a_live_process_alone(tmp_path: Path) -> None:
    registry = tmp_path / "adspower.json"
    registry.write_text(
        json.dumps(
            {
                "alive": owner_of_this_process(),
                "dead": {"pid": 2_000_000_000, "token": "gone"},
                "legacy": None,
            }
        ),
        encoding="utf-8",
    )
    api = FakeAdsPower(active={"alive", "dead", "legacy"})
    provider = adspower(api, registry=registry)

    reaped = await provider.reap_orphans()

    assert reaped == 2
    assert api.active == {"alive"}
    assert set(json.loads(registry.read_text(encoding="utf-8"))) == {"alive"}


async def test_reap_does_not_stop_at_the_first_failure(tmp_path: Path) -> None:
    registry = tmp_path / "adspower.json"
    registry.write_text(json.dumps(["k1", "k2", "k3"]), encoding="utf-8")
    api = FakeAdsPower(
        active={"k1", "k2", "k3"}, errors={"/api/v1/browser/active": (-1, "API busy")}
    )
    provider = adspower(api, registry=registry)

    reaped = await provider.reap_orphans()

    assert reaped == 2
    assert api.active == {"k1"}  # у первого сбой статуса; остальные закрыты
    assert set(json.loads(registry.read_text(encoding="utf-8"))) == {"k1"}  # и остался в реестре


@pytest.mark.parametrize(
    "failure",
    [OSError("connection refused"), TimeoutError(), ValueError("not json")],
    ids=["oserror", "timeout", "garbage"],
)
async def test_transport_failures_are_adspower_errors(failure: Exception) -> None:
    async def broken(url: str, headers: Mapping[str, str], seconds: float) -> bytes:
        _ = url, headers, seconds
        raise failure

    provider = AdsPowerProvider(api_key="secret-key", profile=profile_id, transport=broken)

    with pytest.raises(AdsPowerError) as caught:
        await provider.start(request_for(A))

    assert "secret-key" not in str(caught.value)


async def test_invalid_json_is_an_adspower_error() -> None:
    async def garbage(url: str, headers: Mapping[str, str], seconds: float) -> bytes:
        _ = url, headers, seconds
        return b"<html>502</html>"

    provider = AdsPowerProvider(profile=profile_id, transport=garbage)

    with pytest.raises(AdsPowerError):
        await provider.start(request_for(A))


async def test_local_api_calls_ignore_the_system_proxy(
    fake_driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    with adspower_server(lambda user_id: f"ws://127.0.0.1:1/devtools/browser/{user_id}") as (
        api,
        seen,
    ):
        provider = AdsPowerProvider(api=api, profile=profile_id)
        async with make_pool(fake_driver, provider) as pool, pool.page(A):
            pass

    assert [path for path, _ in seen] == ["/api/v1/browser/start", "/api/v1/browser/stop"]
