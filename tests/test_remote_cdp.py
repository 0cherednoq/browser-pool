"""`RemoteCDP`: адреса по слотам, `/json/version`, и пул поверх чужого Chrome — на двух драйверах."""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Generator, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast, override

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Limits, Topology
from browser_pool.driver import LaunchSpec
from browser_pool.errors import NoFreeEndpointError
from browser_pool.procguard import kill_tree
from browser_pool.provider import EndpointProvider, EndpointRequest
from browser_pool.providers.remote_cdp import RemoteCDP

A = Identity(key="mail:a")
CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def request(browser_id: str) -> EndpointRequest:
    return EndpointRequest(browser_id=browser_id, identity=None, spec=LaunchSpec())


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- адреса ----------------------------------------------------------------------------


def test_remote_cdp_is_an_endpoint_provider() -> None:
    assert isinstance(RemoteCDP("ws://a/devtools/browser/1"), EndpointProvider)


@pytest.mark.parametrize("addresses", [(), ("ftp://host:21",)], ids=["none", "scheme"])
def test_bad_addresses_are_rejected(addresses: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="RemoteCDP"):
        RemoteCDP(*addresses)


async def test_slot_prefers_its_own_address_and_gives_it_back() -> None:
    provider = RemoteCDP("ws://a/devtools/browser/1", "ws://b/devtools/browser/2")

    second = await provider.start(request("browser-1"))
    first = await provider.start(request("browser-0"))
    assert (first.url, second.url) == ("ws://a/devtools/browser/1", "ws://b/devtools/browser/2")

    with pytest.raises(NoFreeEndpointError):
        await provider.start(request("browser-2"))
    await provider.stop(first)
    assert (await provider.start(request("browser-2"))).url == first.url
    assert await provider.reap_orphans() == 0


@contextlib.contextmanager
def version_server(ws: str) -> Generator[int]:
    """Фейковый `/json/version`, как у Chrome, — называет себя localhost."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"webSocketDebuggerUrl": ws}).encode()
            self.send_response(200 if self.path == "/json/version" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: Any) -> None:
            _ = format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


async def test_http_address_becomes_the_browser_websocket_on_the_same_host() -> None:
    with version_server("ws://localhost:9222/devtools/browser/abc?x=1") as port:
        provider = RemoteCDP(f"http://127.0.0.1:{port}")
        endpoint = await provider.start(request("browser-0"))

    assert endpoint.kind == "cdp"
    assert endpoint.url == f"ws://127.0.0.1:{port}/devtools/browser/abc?x=1"
    assert endpoint.pid is None  # удалённый браузер не наш: добивать нечего


@contextlib.contextmanager
def token_server(ws: str, *, token: str, prefix: str = "") -> Generator[tuple[int, list[str]]]:
    """`/json/version` за токеном, как у облачных браузеров: без `?token=` — 401."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            path, _, query = self.path.partition("?")
            ok = path == f"{prefix}/json/version" and f"token={token}" in query.split("&")
            body = json.dumps({"webSocketDebuggerUrl": ws}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: Any) -> None:
            _ = format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


async def test_token_in_the_address_reaches_the_request_and_the_websocket() -> None:
    with token_server("ws://localhost:9222/devtools/browser/abc", token="S3CRET") as (port, seen):
        provider = RemoteCDP(f"http://127.0.0.1:{port}?token=S3CRET")
        endpoint = await provider.start(request("browser-0"))

    assert seen == ["/json/version?token=S3CRET"]
    assert endpoint.url == f"ws://127.0.0.1:{port}/devtools/browser/abc?token=S3CRET"


async def test_address_with_a_path_prefix_and_query_keeps_both() -> None:
    with token_server("ws://localhost/devtools/browser/abc", token="T", prefix="/chrome") as (
        port,
        seen,
    ):
        provider = RemoteCDP(f"http://127.0.0.1:{port}/chrome/?token=T")
        endpoint = await provider.start(request("browser-0"))

    assert seen == ["/chrome/json/version?token=T"]
    assert endpoint.url.endswith("/devtools/browser/abc?token=T")


async def test_query_of_the_browser_and_of_the_address_are_joined() -> None:
    with token_server("ws://localhost/devtools/browser/abc?session=7", token="T") as (port, _):
        provider = RemoteCDP(f"http://127.0.0.1:{port}?token=T")
        endpoint = await provider.start(request("browser-0"))

    assert endpoint.url == f"ws://127.0.0.1:{port}/devtools/browser/abc?session=7&token=T"


async def test_version_request_ignores_the_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    with version_server("ws://localhost:9222/devtools/browser/abc") as port:
        endpoint = await RemoteCDP(f"http://127.0.0.1:{port}").start(request("browser-0"))

    assert endpoint.url == f"ws://127.0.0.1:{port}/devtools/browser/abc"


async def test_failure_text_does_not_carry_the_token() -> None:
    provider = RemoteCDP(f"http://127.0.0.1:{free_port()}?token=S3CRET", timeout=2.0)

    with pytest.raises(OSError) as caught:  # noqa: PT011 — отказ соединения
        await provider.start(request("browser-0"))

    assert "S3CRET" not in str(caught.value)
    assert "S3CRET" not in repr(caught.value)


async def test_wrong_token_is_an_error_without_the_token() -> None:
    with token_server("ws://localhost/devtools/browser/abc", token="RIGHT") as (port, _):
        provider = RemoteCDP(f"http://127.0.0.1:{port}?token=WRONG")
        with pytest.raises(OSError) as caught:  # noqa: PT011 — отказ API
            await provider.start(request("browser-0"))

    assert "WRONG" not in str(caught.value)


async def test_unreachable_address_is_given_back() -> None:
    provider = RemoteCDP(f"http://127.0.0.1:{free_port()}", timeout=2.0)

    for _ in range(2):  # адрес не застревает занятым после сбоя
        with pytest.raises(OSError):  # noqa: PT011 — отказ соединения
            await provider.start(request("browser-0"))


# --- пул поверх чужого Chrome -------------------------------------------------------------


@pytest.fixture
def remote_chrome() -> Iterator[str]:
    """Chrome, запущенный не пулом, с открытым портом отладки; после теста — убит."""
    binary = next((path for path in CHROME_PATHS if Path(path).exists()), None) or shutil.which(
        "chrome"
    )
    if binary is None:
        pytest.skip("нет Chrome")
    port = free_port()
    # Дочерние процессы Chrome отпускают файлы профиля не сразу: остаток каталога тесту не важен.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        process = subprocess.Popen(
            [
                binary,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_port(port)
            yield f"http://127.0.0.1:{port}"
            assert process.poll() is None, "пул закрыл чужой браузер"
        finally:
            kill_tree(process.pid)  # всё дерево: иначе дочерние процессы держат профиль
            process.wait(timeout=10)


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
            return
        time.sleep(0.1)
    pytest.fail("Chrome не открыл порт отладки")


def config() -> PoolConfig:
    return PoolConfig(topology=Topology(browsers=1), limits=Limits(spawn_delay=0.0))


@pytest.mark.browser
async def test_playwright_leases_in_a_remote_chrome(remote_chrome: str) -> None:
    pytest.importorskip("playwright.async_api")
    from browser_pool.drivers.playwright import PlaywrightDriver

    pool = BrowserPool(PlaywrightDriver(), config=config(), provider=RemoteCDP(remote_chrome))
    async with pool, pool.page(A) as lease:
        await lease.page.set_content("<h1>удалённый</h1>")
        assert await lease.page.inner_text("h1") == "удалённый"


@pytest.mark.browser
@pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction':DeprecationWarning")
async def test_pydoll_leases_in_a_remote_chrome(remote_chrome: str) -> None:
    pytest.importorskip("pydoll")
    from browser_pool.drivers.pydoll import PydollDriver

    pool = BrowserPool(PydollDriver(), config=config(), provider=RemoteCDP(remote_chrome))
    async with pool, pool.page(A) as lease:
        response = await lease.page.execute_script("1 + 1", return_by_value=True)
        assert cast("dict[str, Any]", response)["result"]["result"]["value"] == 2
        assert lease.context.id  # отдельный контекст в чужом браузере
