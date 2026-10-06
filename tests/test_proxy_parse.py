"""`Proxy.parse` (M8.07): секреты не попадают в ошибки, форматы продавцов разбираются или отвергаются явно."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from browser_pool.proxies import Proxy, ProxyFormatError

SECRET_ALPHABET = st.characters(min_codepoint=33, max_codepoint=126)
"""Печатные ASCII без пробела: пароль, как его вводят люди и генерируют сервисы."""


def secret(chars: str) -> str:
    """Пароль с меткой, которой нет ни в одном сообщении об ошибке."""
    return "Zq~" + chars


MALFORMED = [
    "user:{pw}:1.2.3.4:notaport",
    "1.2.3.4:notaport:user:{pw}",
    "user:{pw}@1.2.3.4",
    "user:{pw}@1.2.3.4:notaport",
    "{pw}://user@1.2.3.4:80",
    "user:{pw}://x@1.2.3.4:80",
    "1.2.3.4:80:user:{pw}:extra:more",
    "a:1:b:{pw}",
    "1.2.3.4:8080:user:{pw}@ss:1",
    "http://user:{pw}@:80",
    "http://user:{pw}@bad host:80",
    "http://:{pw}@1.2.3.4:80",
    "[::1:{pw}",
]


@given(chars=st.text(SECRET_ALPHABET, min_size=6, max_size=24))
@pytest.mark.parametrize("template", MALFORMED)
def test_no_fragment_of_the_password_reaches_the_error_text(template: str, chars: str) -> None:
    password = secret(chars)
    line = template.format(pw=password)

    try:
        Proxy.parse(line)
    except ProxyFormatError as error:
        message = str(error)
        assert password not in message
        assert "Zq~" not in message
        assert chars not in message
    # Разобралось — тоже допустимо (например, пароль, неожиданно давший валидный вид); утечки нет.


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # продавцы
        (
            "user:pass:1.2.3.4:8080",
            Proxy(host="1.2.3.4", port=8080, username="user", password="pass"),
        ),
        (
            "1.2.3.4:8080:user:pass",
            Proxy(host="1.2.3.4", port=8080, username="user", password="pass"),
        ),
        (
            "1.2.3.4:8080@user:pass",
            Proxy(host="1.2.3.4", port=8080, username="user", password="pass"),
        ),
        (
            "user:pass@1.2.3.4:8080",
            Proxy(host="1.2.3.4", port=8080, username="user", password="pass"),
        ),
        (
            "gate.example.com:7000:user-session-1:pw",
            Proxy(host="gate.example.com", port=7000, username="user-session-1", password="pw"),
        ),
        (
            "user:12345:1.2.3.4:8080",
            Proxy(host="1.2.3.4", port=8080, username="user", password="12345"),
        ),
        (
            "1.2.3.4:8080:user:12345",
            Proxy(host="1.2.3.4", port=8080, username="user", password="12345"),
        ),
        (
            "1.2.3.4:8080@user:12345",
            Proxy(host="1.2.3.4", port=8080, username="user", password="12345"),
        ),
        (
            "user:12345@gate.example.com:8080",
            Proxy(host="gate.example.com", port=8080, username="user", password="12345"),
        ),
        # URL: спецсимволы пароля
        (
            "http://user:p@ss@host.example:80",
            Proxy(host="host.example", port=80, username="user", password="p@ss"),
        ),
        (
            "http://user:pa:ss@host.example:80",
            Proxy(host="host.example", port=80, username="user", password="pa:ss"),
        ),
        (
            "http://user:pa/ss@host.example:80",
            Proxy(host="host.example", port=80, username="user", password="pa/ss"),
        ),
        (
            "http://user:p%40ss%3A1%2F%25@host.example:80/",
            Proxy(host="host.example", port=80, username="user", password="p@ss:1/%"),
        ),
        (
            "user:pa/@host.example:80",
            Proxy(host="host.example", port=80, username="user", password="pa/"),
        ),
        (
            "http://user:pa/@host.example:80/",
            Proxy(host="host.example", port=80, username="user", password="pa/"),
        ),
        # двоеточечный вид: пароль как есть, без декодирования и без срезания '/'
        (
            "1.2.3.4:8080:user:pa/",
            Proxy(host="1.2.3.4", port=8080, username="user", password="pa/"),
        ),
        (
            "1.2.3.4:8080:user:%41",
            Proxy(host="1.2.3.4", port=8080, username="user", password="%41"),
        ),
        # в виде с @ — декодируется, как в URL
        ("user:%41@1.2.3.4:8080", Proxy(host="1.2.3.4", port=8080, username="user", password="A")),
        # схема в пароле не принимается за схему прокси
        (
            "user:se://cret@host.example:80",
            Proxy(host="host.example", port=80, username="user", password="se://cret"),
        ),
        # регистр хоста
        ("HOST.Example.COM:8080", Proxy(host="host.example.com", port=8080)),
        ("HTTP://Host.Example:80/", Proxy(host="host.example", port=80)),
        ("[::A]:3128", Proxy(host="::a", port=3128)),
        ("[::1]:80:user:pass", Proxy(host="::1", port=80, username="user", password="pass")),
    ],
)
def test_vendor_formats_are_parsed(line: str, expected: Proxy) -> None:
    assert Proxy.parse(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "a:1:b:2",  # оба вида возможны, IP нет — не угадываем
        "user:pass:host:80:extra",
        "host:80:user:pa:ss",  # пароль с ':' — только в виде URL
        "1.2.3.4:8080:user:p@ss:1",  # похоже на двоеточечный вид с '@' в пароле
        "host:80:user",
        "host",
        "host:0",
        "host:65536",
        "host:٨٠",  # не ASCII-цифры
        "http://:80",
        "ftp://host:21",
        "[::1",
        "http://user:pass@",
    ],
)
def test_ambiguous_or_broken_lines_are_rejected_not_guessed(line: str) -> None:
    with pytest.raises(ProxyFormatError):
        Proxy.parse(line)


@given(
    username=st.text(SECRET_ALPHABET, min_size=1, max_size=12),
    password=st.text(SECRET_ALPHABET, min_size=1, max_size=24),
)
def test_url_of_a_proxy_parses_back_to_the_same_credentials(username: str, password: str) -> None:
    proxy = Proxy(host="gate.example.com", port=8080, username=username, password=password)

    assert Proxy.parse(proxy.url) == proxy


def test_host_is_lowercased_by_the_constructor_too() -> None:
    assert Proxy(host="Gate.Example.COM", port=80) == Proxy.parse("gate.example.com:80")
