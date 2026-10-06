"""Улики сбоев и метрики Prometheus."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from browser_pool import BrowserPool, Identity, PoolConfig
from browser_pool.config import Lifecycle, Limits, Topology
from browser_pool.driver import Evidence
from browser_pool.events import LeaseReleased
from browser_pool.evidence import DirectoryEvidenceSink, EvidenceSink, image_suffix
from browser_pool.testing import FakeBrowser, FakeContext, FakeDriver, FakePage
from browser_pool.testing.fake_driver import FakeTargetClosedError

type Pool = BrowserPool[FakeBrowser, FakeContext, FakePage]

A = Identity(key="mail:a/1")


def make_pool(driver: FakeDriver, sink: EvidenceSink | None = None) -> Pool:
    return BrowserPool(
        driver,
        config=PoolConfig(
            topology=Topology(browsers=1),
            limits=Limits(spawn_delay=0.0),
            lifecycle=Lifecycle(healthcheck_interval=1.0),
        ),
        evidence_sink=sink,
    )


async def crash(pool: Pool, identity: Identity, url: str) -> None:
    async with pool.page(identity) as lease:
        lease.page.url = url
        msg = "вкладка умерла"
        raise FakeTargetClosedError(msg)


async def fail(pool: Pool, identity: Identity = A, url: str = "https://mail.example/inbox") -> None:
    with pytest.raises(FakeTargetClosedError):
        await crash(pool, identity, url)


# --- улики ------------------------------------------------------------------------------


async def test_failed_lease_leaves_evidence(fake_driver: FakeDriver, tmp_path: Path) -> None:
    released: list[LeaseReleased] = []
    pool = make_pool(fake_driver, DirectoryEvidenceSink(tmp_path))
    pool.on(LeaseReleased, released.append)

    async with pool:
        await fail(pool)
        async with pool.page(A):
            pass

    failed, ok = released
    assert failed.evidence is not None
    assert ok.evidence is None  # без сбоя улик нет
    meta = json.loads(Path(failed.evidence).read_text(encoding="utf-8"))  # ссылка — файл метаданных
    assert meta["key"] == "mail:a/1"
    assert meta["error"] == "FakeTargetClosedError"
    assert meta["url"] == "https://mail.example/inbox"  # снято до того, как вкладку выбросили
    assert "mail_a_1" in Path(failed.evidence).name  # ключ безопасен как имя файла
    assert Path(failed.evidence).exists()


async def test_only_the_latest_evidence_is_kept(fake_driver: FakeDriver, tmp_path: Path) -> None:
    async with make_pool(fake_driver, DirectoryEvidenceSink(tmp_path, keep=2)) as pool:
        for _ in range(3):
            await fail(pool)
            await asyncio.sleep(1.1)  # имена по времени — разные секунды

    assert len(list(tmp_path.glob("*.json"))) == 2


async def test_files_hold_screenshot_and_html(tmp_path: Path) -> None:
    sink = DirectoryEvidenceSink(tmp_path)
    evidence = Evidence(screenshot=b"\x89PNG", html="<p>ящик</p>", url="https://x")

    base = Path(await sink.save(evidence, key="mail:a", lease_id=7, error="ValueError"))

    assert base.with_suffix(".png").read_bytes() == b"\x89PNG"
    assert base.with_suffix(".html").read_text(encoding="utf-8") == "<p>ящик</p>"


class BrokenSink:
    async def save(self, evidence: Evidence, *, key: str, lease_id: int, error: str) -> str:
        _ = evidence, key, lease_id, error
        msg = "диск полон"
        raise OSError(msg)


async def test_broken_sink_does_not_hide_the_error(
    fake_driver: FakeDriver, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        async with make_pool(fake_driver, BrokenSink()) as pool:
            await fail(pool)

    assert "Улики аренды" in caplog.text


# --- Prometheus -------------------------------------------------------------------------


async def test_metrics_follow_the_events(fake_driver: FakeDriver) -> None:
    pytest.importorskip("prometheus_client")
    from prometheus_client import CollectorRegistry

    from browser_pool.monitors.prometheus import PrometheusMetrics

    registry = CollectorRegistry()
    pool = make_pool(fake_driver)
    PrometheusMetrics(registry=registry).attach(pool, name="main")

    async with pool:
        async with pool.page(A):
            pass
        await fail(pool)
        await asyncio.sleep(1.5)  # проверка здоровья → PoolHealth

    def value(name: str, **labels: str) -> float | None:
        return registry.get_sample_value(f"browser_pool_{name}", labels)

    assert value("leases_total", pool="main", outcome="ok") == 1
    assert value("leases_total", pool="main", outcome="page") == 1
    assert value("contexts_opened_total", pool="main") == 1
    assert value("browser_events_total", pool="main", event="started") == 1
    assert value("lease_wait_seconds_count", pool="main") == 2
    assert value("capacity", pool="main", scope="total") == 8
    assert value("browsers", pool="main", state="healthy") == 1
    assert value("under_pressure", pool="main") == 0


@pytest.mark.parametrize(
    ("data", "suffix"),
    [
        (b"\x89PNG\r\n\x1a\n...", ".png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", ".jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp"),
        (b"GIF89a", ".img"),
    ],
)
def test_screenshot_suffix_follows_the_format(data: bytes, suffix: str) -> None:
    assert image_suffix(data) == suffix


async def test_jpeg_screenshot_is_saved_as_jpg_and_pruned(tmp_path: Path) -> None:
    sink = DirectoryEvidenceSink(tmp_path, keep=1)
    jpeg = b"\xff\xd8\xff\xe0"
    first = await sink.save(
        Evidence(screenshot=jpeg, url="https://x"), key="k", lease_id=1, error="E"
    )
    assert Path(first).with_suffix(".jpg").read_bytes() == jpeg
    await sink.save(Evidence(url="https://x"), key="k", lease_id=2, error="E")
    assert not Path(first).with_suffix(".jpg").exists()


# --- имена файлов (M8.18, §4 п. 2) -----------------------------------------------------------


async def test_dots_in_the_key_do_not_eat_the_lease_number(tmp_path: Path) -> None:
    sink = DirectoryEvidenceSink(tmp_path)
    evidence = Evidence(screenshot=b"\x89PNG", html="<p/>", url="https://x")

    first = Path(await sink.save(evidence, key="ada@example.com", lease_id=5, error="E"))
    second = Path(await sink.save(evidence, key="ada@example.com", lease_id=6, error="E"))

    assert first != second  # раньше `with_suffix` срезал `.com-5` — комплекты затирали друг друга
    for reference in (first, second):
        assert reference.exists()
        stem = reference.name.removesuffix(".json")
        assert (tmp_path / f"{stem}.png").exists()
        assert (tmp_path / f"{stem}.html").exists()
    assert first.name.removesuffix(".json").endswith("-5")
    assert second.name.removesuffix(".json").endswith("-6")


async def test_keys_that_sanitize_alike_get_different_files(tmp_path: Path) -> None:
    sink = DirectoryEvidenceSink(tmp_path)
    evidence = Evidence(url="https://x")

    one = await sink.save(evidence, key="mail:a", lease_id=1, error="E")
    two = await sink.save(evidence, key="mail_a", lease_id=1, error="E")

    assert one != two


async def test_pruning_works_with_dotted_names(tmp_path: Path) -> None:
    sink = DirectoryEvidenceSink(tmp_path, keep=2)
    for lease_id in range(4):
        await sink.save(
            Evidence(screenshot=b"\x89PNG", url="https://x"),
            key="ada@example.com",
            lease_id=lease_id,
            error="E",
        )

    assert len(list(tmp_path.glob("*.json"))) == 2
    assert len(list(tmp_path.glob("*.png"))) == 2
