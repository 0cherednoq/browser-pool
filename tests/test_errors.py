"""Ошибки пула и виды сбоев: иерархия, данные на ошибке, секреты, переносимость."""

from __future__ import annotations

import logging
import math
import pickle
from dataclasses import dataclass

import pytest

from browser_pool import ErrorKind, PoolSignal
from browser_pool.errors import (
    AcquireTimeoutError,
    Classification,
    HasErrorKind,
    IdentityBlockedError,
    IdentityCoolingDownError,
    LeaseRevokedError,
    NoFreeEndpointError,
    NoUsableProxyError,
    PoolError,
    PoolInvariantError,
    PoolSaturatedError,
    PoolStoppedError,
    PoolUnavailableError,
    ProxyFailedError,
    StaleLeaseError,
    StartupTimeoutError,
    UnsupportedRequirementError,
    declared_classification,
    error_chain,
)
from browser_pool.proxies import Proxy

SECRET_PROXY = Proxy(
    host="10.0.0.7",
    port=1080,
    scheme="socks5",
    username="user-session-42",
    password="hunter2",
    id="db-17",
)


def every_pool_error() -> list[PoolError]:
    """По экземпляру каждой ошибки пула — с непустыми данными."""
    return [
        PoolStoppedError(),
        PoolSaturatedError(max_waiting=200),
        AcquireTimeoutError(timeout=30.0, candidates=("mail:1", "mail:2")),
        PoolUnavailableError(),
        PoolInvariantError("слот выдан дважды"),
        StaleLeaseError(lease_id=9, generation=3, current=4),
        LeaseRevokedError(lease_id=9, identity="mail:a", held=900.0),
        StartupTimeoutError(timeout=30.0),
        UnsupportedRequirementError(missing=("proxy_auth", "socks5")),
        NoUsableProxyError(identity="mail:1"),
        NoFreeEndpointError(addresses=2),
        ProxyFailedError(
            proxy=SECRET_PROXY, identity="mail:1", reason="ERR_PROXY_CONNECTION_FAILED"
        ),
        IdentityCoolingDownError(identity="mail:1", retry_after=40.0),
        IdentityBlockedError(identity="mail:1", reason="челлендж"),
    ]


# --- иерархия -------------------------------------------------------------------------


@pytest.mark.parametrize("error", every_pool_error(), ids=lambda error: type(error).__name__)
def test_every_pool_error_is_a_pool_error(error: PoolError) -> None:
    assert isinstance(error, PoolError)
    assert str(error)


def test_acquire_timeout_is_also_a_timeout() -> None:
    # `except TimeoutError` у вызывающего ловит и таймаут `asyncio.timeout`, и таймаут пула.
    assert issubclass(AcquireTimeoutError, TimeoutError)


def test_signal_is_not_a_pool_error() -> None:
    # Сигнал бросает site SDK, а не пул: `except PoolError` не должен его глотать.
    assert not issubclass(PoolSignal, PoolError)


# --- данные на ошибке ------------------------------------------------------------------


def test_proxy_failure_carries_the_proxy_itself() -> None:
    error = ProxyFailedError(proxy=SECRET_PROXY, identity="mail:1", reason="timeout")

    assert error.proxy is SECRET_PROXY
    assert error.identity == "mail:1"
    assert error.reason == "timeout"


def test_unsupported_requirement_lists_everything_missing() -> None:
    error = UnsupportedRequirementError(missing=("proxy_auth", "socks5", "new_window"))

    assert error.missing == ("proxy_auth", "socks5", "new_window")
    assert all(item in str(error) for item in error.missing)


def test_unsupported_requirement_needs_something_missing() -> None:
    with pytest.raises(ValueError, match="несоответств"):
        UnsupportedRequirementError(missing=())


def test_timeout_message_does_not_grow_with_candidates() -> None:
    candidates = tuple(f"mail:{number}" for number in range(1000))

    message = str(AcquireTimeoutError(timeout=5.0, candidates=candidates))

    assert "mail:0" in message
    assert "mail:999" not in message
    assert "995" in message  # «…и ещё 995»


def test_cooling_down_rejects_negative_wait() -> None:
    with pytest.raises(ValueError, match="retry_after"):
        IdentityCoolingDownError(identity="mail:1", retry_after=-1.0)


# --- секреты ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", every_pool_error(), ids=lambda error: type(error).__name__)
def test_no_proxy_credentials_in_text_or_repr(error: PoolError) -> None:
    rendered = f"{error} {error!r} {error.args!r}"

    assert "hunter2" not in rendered
    assert "user-session-42" not in rendered


def test_proxy_failure_names_the_proxy_safely() -> None:
    assert "db-17" in str(ProxyFailedError(proxy=SECRET_PROXY, identity="mail:1", reason="timeout"))


# --- переносимость ---------------------------------------------------------------------


@pytest.mark.parametrize("error", every_pool_error(), ids=lambda error: type(error).__name__)
def test_pool_errors_survive_pickling(error: PoolError) -> None:
    # Ошибки уходят через границы процессов: результаты воркеров, очереди задач.
    restored = pickle.loads(pickle.dumps(error))  # noqa: S301 — свои же байты

    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert vars(restored) == vars(error)


class SessionExpired(PoolSignal):
    """Сигнал site SDK с видом по умолчанию."""

    default_kind = ErrorKind.session


def test_signals_survive_pickling() -> None:
    restored = pickle.loads(pickle.dumps(SessionExpired("вышли из ящика")))  # noqa: S301

    assert type(restored) is SessionExpired
    assert restored.kind is ErrorKind.session
    assert str(restored) == "вышли из ящика"


# --- сигнал site SDK -------------------------------------------------------------------


def test_signal_kind_from_argument() -> None:
    signal = PoolSignal("429", kind=ErrorKind.rate_limited, retry_after=40.0)

    assert signal.kind is ErrorKind.rate_limited
    assert signal.retry_after == 40.0


def test_signal_kind_from_subclass_default() -> None:
    assert SessionExpired().kind is ErrorKind.session


def test_signal_argument_overrides_subclass_default() -> None:
    assert SessionExpired(kind=ErrorKind.blocked).kind is ErrorKind.blocked


def test_signal_accepts_kind_as_plain_string() -> None:
    # SDK может не импортировать ErrorKind: значения — обычные строки.
    assert PoolSignal(kind="proxy").kind is ErrorKind.proxy


def test_signal_without_kind_is_a_programming_error() -> None:
    with pytest.raises(TypeError, match="kind"):
        PoolSignal("что-то сломалось")


def test_signal_with_unknown_kind_fails_where_it_is_written() -> None:
    with pytest.raises(ValueError, match="sesion"):
        PoolSignal(kind="sesion")


@pytest.mark.parametrize(
    ("kind", "retry_after"),
    [
        (ErrorKind.session, 10.0),  # ждать имеет смысл только при rate_limited
        (ErrorKind.rate_limited, -1.0),
        (ErrorKind.rate_limited, math.nan),
        (ErrorKind.rate_limited, math.inf),
    ],
)
def test_signal_rejects_meaningless_retry_after(kind: ErrorKind, retry_after: float) -> None:
    with pytest.raises(ValueError, match="retry_after"):
        PoolSignal(kind=kind, retry_after=retry_after)


# --- чтение объявленного вида ----------------------------------------------------------


@dataclass
class ForeignRateLimitError(Exception):
    """Ошибка чужого SDK, который про browser_pool не знает — только про протокол."""

    pool_error_kind: str = "rate_limited"
    pool_retry_after: object = 12.5


def test_signal_is_read_as_declared() -> None:
    signal = PoolSignal(kind=ErrorKind.rate_limited, retry_after=3.0)

    assert declared_classification(signal) == Classification(ErrorKind.rate_limited, 3.0)


def test_foreign_error_implementing_the_protocol_is_read() -> None:
    error = ForeignRateLimitError()

    assert isinstance(error, HasErrorKind)
    assert declared_classification(error) == Classification(ErrorKind.rate_limited, 12.5)


def test_plain_error_declares_nothing() -> None:
    assert declared_classification(RuntimeError("boom")) is None


def test_foreign_unknown_kind_becomes_unknown_and_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Опечатка в чужом SDK не должна ни маскировать исходную ошибку, ни пройти молча.
    with caplog.at_level(logging.WARNING, logger="browser_pool"):
        verdict = declared_classification(ForeignRateLimitError(pool_error_kind="sesion"))

    assert verdict == Classification(ErrorKind.unknown)
    assert "sesion" in caplog.text


@pytest.mark.parametrize("retry_after", ["40", -5, math.nan, True])
def test_foreign_bad_retry_after_is_dropped_and_reported(
    retry_after: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="browser_pool"):
        verdict = declared_classification(ForeignRateLimitError(pool_retry_after=retry_after))

    assert verdict == Classification(ErrorKind.rate_limited)
    assert "retry_after" in caplog.text


def test_foreign_retry_after_is_ignored_for_other_kinds(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="browser_pool"):
        verdict = declared_classification(ForeignRateLimitError(pool_error_kind="session"))

    assert verdict == Classification(ErrorKind.session)
    assert "retry_after" in caplog.text


# --- цепочка причин ---------------------------------------------------------------------


def raised_from[E: BaseException](outer: E, cause: BaseException) -> E:
    """Как `raise outer from cause`."""
    outer.__cause__ = cause
    return outer


def raised_during[E: BaseException](outer: E, context: BaseException) -> E:
    """Как `raise outer` внутри `except` для `context`."""
    outer.__context__ = context
    return outer


def test_chain_follows_explicit_cause_then_implicit_context() -> None:
    root = KeyError("корень")
    middle = raised_during(ValueError("середина"), root)
    outer = raised_from(RuntimeError("снаружи"), middle)

    assert list(error_chain(outer)) == [outer, middle, root]


def test_chain_stops_where_context_is_suppressed() -> None:
    hidden = KeyError("скрыт")
    outer = RuntimeError("снаружи")
    outer.__context__, outer.__suppress_context__ = hidden, True  # как после `raise ... from None`

    assert list(error_chain(outer)) == [outer]


def test_chain_survives_a_cycle() -> None:
    first, second = RuntimeError("1"), RuntimeError("2")
    first.__cause__, second.__cause__ = second, first

    assert list(error_chain(first)) == [first, second]


def test_declaration_is_read_from_a_wrapped_cause() -> None:
    outer = raised_from(RuntimeError("SDK завернул"), PoolSignal(kind=ErrorKind.session))

    assert declared_classification(outer) == Classification(ErrorKind.session)


def test_outer_declaration_wins_over_its_cause() -> None:
    inner = PoolSignal(kind=ErrorKind.page)
    outer = raised_from(PoolSignal(kind=ErrorKind.blocked), inner)

    assert declared_classification(outer) == Classification(ErrorKind.blocked)
