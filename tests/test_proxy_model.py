"""Прокси как значение: адрес, креды, безопасное имя. Разбор строк — M2.03."""

from __future__ import annotations

import pytest

from browser_pool.proxies import Proxy


def test_url_carries_credentials_and_server_does_not() -> None:
    proxy = Proxy(host="1.2.3.4", port=8080, username="user", password="pa:ss@word")

    assert proxy.server == "http://1.2.3.4:8080"
    # Креды экранируются: `:` и `@` в пароле не должны ломать разбор URL на той стороне.
    assert proxy.url == "http://user:pa%3Ass%40word@1.2.3.4:8080"
    assert proxy.has_auth


def test_url_without_credentials_is_the_server() -> None:
    proxy = Proxy(host="proxy.local", port=3128)

    assert proxy.url == proxy.server == "http://proxy.local:3128"
    assert not proxy.has_auth


def test_ipv6_host_is_bracketed() -> None:
    assert Proxy(host="::1", port=1080, scheme="socks5").server == "socks5://[::1]:1080"


def test_label_prefers_source_id() -> None:
    assert Proxy(host="1.2.3.4", port=80, id="db-17").label == "db-17"
    assert Proxy(host="1.2.3.4", port=80).label == "http://1.2.3.4:80"


def test_credentials_never_rendered() -> None:
    proxy = Proxy(host="1.2.3.4", port=80, username="user-session-42", password="hunter2")

    rendered = f"{proxy} {proxy!r}"

    assert "hunter2" not in rendered
    assert "user-session-42" not in rendered
    assert "1.2.3.4" in rendered


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"host": "", "port": 80}, "host"),
        ({"host": "user:pass@1.2.3.4", "port": 80}, "host"),
        ({"host": "http://1.2.3.4", "port": 80}, "host"),
        ({"host": "1.2.3.4", "port": 0}, "port"),
        ({"host": "1.2.3.4", "port": 65536}, "port"),
        ({"host": "1.2.3.4", "port": 80, "scheme": "ftp"}, "ftp"),
        ({"host": "1.2.3.4", "port": 80, "password": "orphan"}, "password"),
    ],
)
def test_invalid_proxy_rejected(kwargs: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        Proxy(**kwargs)  # pyright: ignore[reportArgumentType] — таблица невалидных входов
