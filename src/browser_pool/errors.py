"""Ошибки пула и виды сбоев.

Здесь две разные вещи.

**Ошибки пула** (`PoolError` и наследники) пул бросает сам: не дождались страницы, пул
остановлен, прокси не прошёл. У каждой — данные, по которым вызывающий решает, что
делать дальше (`ProxyFailedError.proxy` — какой прокси пометить сбойным).

**Вид сбоя** (`ErrorKind`) — ответ на вопрос «что сломалось» про *чужую* ошибку: ту, что
вылетела из кода site SDK посреди аренды. Пул её не меняет и не глотает, а только
реагирует: выбросить вкладку, пересоздать контекст, сменить прокси. Объявить вид SDK может
двумя способами — бросить `PoolSignal` или дать своей ошибке атрибут `pool_error_kind`
(протокол `HasErrorKind`). Второй способ не требует импортировать `browser_pool` вовсе.
Объявление читается по всей цепочке причин (`error_chain`): завёрнутая ошибка его не теряет.

Ошибки ходят через границы процессов (результаты воркеров, очереди задач), поэтому все
они переживают `pickle` вместе со своими данными. Секреты — креды прокси — в текст и
`repr` ошибок не попадают: прокси называется только своим `label`.
"""

from __future__ import annotations

import logging
import math

# В рантайме: псевдоним `Classifier` вычисляется по запросу (`.__value__`), и имя должно существовать.
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol, override, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator

    from browser_pool.proxies import Proxy

_logger = logging.getLogger(__name__)

_SHOWN_CANDIDATES = 5
"""Сколько identity называть в тексте таймаута: список кандидатов бывает на тысячи."""


class ErrorKind(StrEnum):
    """Что сломалось - и значит, что пулу делать с ресурсами аренды."""

    page = "page"
    """Вкладка в неизвестном состоянии: выбросить её, контекст жив."""

    session = "session"
    """Сессия протухла: контекст на выход, следующий вход - через логин."""

    proxy = "proxy"
    """Прокси не пропустил: отчитаться источнику, контекст на выход, открыть с другим прокси."""

    rate_limited = "rate_limited"
    """Сайт просит подождать: identity на паузу на `retry_after`, контекст и сессия живы."""

    blocked = "blocked"
    """Бан или челлендж: identity заблокировать, автоматически не переоткрывать."""

    browser = "browser"
    """Процесс браузера умер или завис: карантин браузера целиком."""

    unknown = "unknown"
    """Непонятно что: выбросить только вкладку."""


def _check_retry_after(kind: ErrorKind, retry_after: float | None) -> None:
    if retry_after is None:
        return
    if kind is not ErrorKind.rate_limited:
        msg = f"retry_after имеет смысл только для {ErrorKind.rate_limited}, а не для {kind}"
        raise ValueError(msg)
    if not math.isfinite(retry_after) or retry_after < 0:
        msg = f"retry_after должен быть конечным и неотрицательным, получено {retry_after}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Classification:
    """Вид сбоя и, для `rate_limited`, сколько секунд ждать. `None` - пауза по умолчанию пула."""

    kind: ErrorKind
    retry_after: float | None = None

    def __post_init__(self) -> None:
        _check_retry_after(self.kind, self.retry_after)


@runtime_checkable
class HasErrorKind(Protocol):
    """Ошибка, которая сама говорит, что сломалось. Импортировать `browser_pool` для этого не нужно.

    `pool_error_kind` - значение `ErrorKind` или его строка (`"session"`). Для
    `rate_limited` ошибка может дать и атрибут `pool_retry_after` - секунды паузы.
    """

    @property
    def pool_error_kind(self) -> str:
        """Вид сбоя: значение `ErrorKind`."""
        ...


_CHAIN_LIMIT = 32
"""Сколько звеньев цепочки причин смотреть: цепочки бывают зациклены или огромны."""


type Classifier = Callable[[BaseException], ErrorKind | Classification | None]
"""Классификатор пула: вид сбоя, вид с паузой (`Classification`) или `None` — «не моё»."""


def error_chain(error: BaseException) -> Iterator[BaseException]:
    """Ошибка и её причины, снаружи внутрь: `__cause__`, иначе `__context__`.

    Так же идёт `traceback`: `raise ... from e` — явная причина, неявный контекст — если
    его не скрыли `from None`. SDK, завернувший сигнал или ошибку драйвера в свою, не
    теряет её классификацию — как `errors.Is` в Go.
    """
    seen: set[int] = set()
    link: BaseException | None = error
    while link is not None and id(link) not in seen and len(seen) < _CHAIN_LIMIT:
        seen.add(id(link))
        yield link
        if link.__cause__ is not None:
            link = link.__cause__
        elif link.__suppress_context__:
            link = None
        else:
            link = link.__context__


def declared_classification(error: BaseException) -> Classification | None:
    """Вид сбоя, который объявила ошибка или её причина; `None` — никто в цепочке не объявлял.

    Побеждает внешнее объявление: кто завернул ошибку, знает о ней больше. Некорректное
    объявление чужого SDK не бросает исключения — оно бы заслонило исходную ошибку
    арендатора, — а превращается в `unknown` или теряет `retry_after`, с предупреждением
    в лог: опечатка в SDK не должна пройти молча.
    """
    for link in error_chain(error):
        if isinstance(link, HasErrorKind):
            return _declared(link)
    return None


def _declared(error: HasErrorKind) -> Classification:
    try:
        kind = ErrorKind(error.pool_error_kind)
    except ValueError:
        _logger.warning(
            "%s объявляет неизвестный вид сбоя %r; считаю %s",
            type(error).__qualname__,
            error.pool_error_kind,
            ErrorKind.unknown,
        )
        return Classification(ErrorKind.unknown)
    raw: object = getattr(error, "pool_retry_after", None)
    if raw is None:
        return Classification(kind)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        problem = f"retry_after должен быть числом секунд, получено {raw!r}"
    else:
        try:
            return Classification(kind, float(raw))
        except ValueError as invalid:
            problem = str(invalid)
    _logger.warning("%s: %s; пауза по умолчанию", type(error).__qualname__, problem)
    return Classification(kind)


class _PortableError(Exception):
    """Переживает `pickle` со всеми атрибутами, какой бы ни была сигнатура `__init__`.

    Штатный `pickle` пересоздаёт исключение вызовом `cls(*args)`, а у наших ошибок данные
    передаются keyword-only и в `args` не лежат: без этого распаковка упала бы.
    """

    @override
    def __reduce__(
        self,
    ) -> tuple[
        Callable[[type[_PortableError], tuple[object, ...], dict[str, object]], _PortableError],
        tuple[type[_PortableError], tuple[object, ...], dict[str, object]],
    ]:
        return _restore, (type(self), self.args, dict(vars(self)))


def _restore(
    cls: type[_PortableError], args: tuple[object, ...], state: dict[str, object]
) -> _PortableError:
    error = cls.__new__(cls, *args)
    error.args = args
    vars(error).update(state)
    return error


class PoolSignal(_PortableError):  # noqa: N818 — не ошибка пула, а сигнал ему от site SDK
    """Сигнал пулу от site SDK: «вот что сломалось». Пробрасывается наружу как есть.

    Прямо::

        raise PoolSignal("сессия протухла", kind=ErrorKind.session)

    или своим классом с видом по умолчанию::

        class SessionExpired(PoolSignal):
            default_kind = ErrorKind.session
    """

    default_kind: ClassVar[ErrorKind | None] = None
    """Вид для наследника, который не передаёт `kind` при создании."""

    classification: Classification
    """Вид сбоя и пауза - то, что прочитает пул."""

    def __init__(
        self,
        message: str = "",
        *,
        kind: ErrorKind | str | None = None,
        retry_after: float | None = None,
    ) -> None:
        chosen = kind if kind is not None else type(self).default_kind
        if chosen is None:
            msg = f"{type(self).__qualname__}: укажите kind= или default_kind у класса"
            raise TypeError(msg)
        self.classification = Classification(ErrorKind(chosen), retry_after)
        super().__init__(message)

    @property
    def kind(self) -> ErrorKind:
        """Что сломалось."""
        return self.classification.kind

    @property
    def retry_after(self) -> float | None:
        """Сколько секунд ждать — только для `rate_limited`."""
        return self.classification.retry_after

    @property
    def pool_error_kind(self) -> str:
        """Протокол `HasErrorKind`: сигнал читается тем же путём, что и чужие ошибки."""
        return self.kind.value

    @property
    def pool_retry_after(self) -> float | None:
        """Протокол `HasErrorKind`: пауза для `rate_limited`."""
        return self.retry_after


class PoolError(_PortableError):
    """Ошибка самого пула. `except PoolError` ловит всё, что бросает пул, и ничего чужого."""


class ConfigError(PoolError, ValueError):
    """Конфиг невалиден. Падает при создании конфига, а не в рантайме пула.

    Перечисляет все найденные проблемы разом - с путём до поля (`limits.max_waiting`).
    """

    problems: tuple[str, ...]
    """Каждая проблема отдельной строкой."""

    def __init__(self, *problems: str) -> None:
        if not problems:
            msg = "ConfigError без проблем: нечего сообщать"
            raise ValueError(msg)
        self.problems = problems
        super().__init__("Конфиг пула невалиден:\n  - " + "\n  - ".join(problems))


class PoolStoppedError(PoolError):
    """Пул не запущен или уже останавливается: новых аренд не будет."""

    def __init__(self, message: str = "Пул остановлен или ещё не запущен") -> None:
        super().__init__(message)


class PoolSaturatedError(PoolError):
    """Очередь ожидания полна (`Limits.max_waiting`): отказ сразу, а не зависание."""

    max_waiting: int
    """Длина очереди, в которую запрос не поместился."""

    def __init__(self, *, max_waiting: int) -> None:
        self.max_waiting = max_waiting
        super().__init__(f"Очередь ожидания страниц полна: {max_waiting} ожидающих")


class StartupTimeoutError(PoolError, TimeoutError):
    """Драйвер не подготовился за `Timeouts.startup`: пул не запущен. Это ещё и `TimeoutError`."""

    timeout: float
    """Сколько секунд ждали."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"Драйвер не подготовился к работе за {timeout:g} с — пул не запущен")


class AcquireTimeoutError(PoolError, TimeoutError):
    """Страница не выдана за отведённое время - `acquire_timeout=` аренды или `Timeouts.acquire`.

    Это ещё и `TimeoutError`.
    """

    timeout: float
    """Сколько секунд ждали."""
    candidates: tuple[str, ...]
    """Ключи identity, для которых просили страницу, - все, а не только названные в тексте."""

    def __init__(self, *, timeout: float, candidates: tuple[str, ...]) -> None:
        self.timeout = timeout
        self.candidates = candidates
        shown = ", ".join(candidates[:_SHOWN_CANDIDATES])
        rest = len(candidates) - _SHOWN_CANDIDATES
        if rest > 0:
            shown += f" и ещё {rest}"
        super().__init__(f"Страница не выдана за {timeout:g} с; кандидаты: {shown}")


class PoolUnavailableError(PoolError):
    """Все браузеры нездоровы и восстановление не идёт: ждать бессмысленно."""

    def __init__(
        self, message: str = "Все браузеры нездоровы, и восстановление не выполняется"
    ) -> None:
        super().__init__(message)


class PoolInvariantError(PoolError):
    """Нарушен внутренний инвариант пула. Это ошибка библиотеки, а не ситуация - сообщите о ней."""


class StaleLeaseError(PoolError):
    """Операция по аренде устаревшего поколения: контекст уже пересоздан.

    Действие по такой аренде не выполняется - иначе оно задело бы чужой, новый контекст.
    """

    lease_id: int
    """Устаревшая аренда - тот же номер, что в `lease.lease_id` и событиях."""
    generation: int
    """Поколение контекста, на которое выдана аренда."""
    current: int
    """Поколение контекста сейчас."""

    def __init__(self, *, lease_id: int, generation: int, current: int) -> None:
        self.lease_id = lease_id
        self.generation = generation
        self.current = current
        super().__init__(
            f"Аренда {lease_id} относится к поколению {generation}, контекст уже в поколении {current}"
        )


class LeaseRevokedError(PoolError):
    """Аренда держалась дольше `Limits.lease_max_duration`, и пул её отозвал.

    Вылетает на границе `async with pool.page(...)`: код внутри аренды прерван, вкладка выброшена,
    слот свободен. Задача арендатора при этом не отменена - ошибку можно поймать и работать дальше.
    """

    lease_id: int
    """Отозванная аренда."""
    identity: str
    """Ключ identity аренды."""
    held: float
    """Сколько секунд аренда держалась."""

    def __init__(self, *, lease_id: int, identity: str, held: float) -> None:
        self.lease_id = lease_id
        self.identity = identity
        self.held = held
        super().__init__(
            f"Аренда {lease_id} ({identity}) отозвана: держалась {held:g} с — дольше lease_max_duration"
        )


class UnsupportedRequirementError(PoolError):
    """Запрос невыполним на этом драйвере - никогда, сколько ни жди. Перечислено всё недостающее."""

    missing: tuple[str, ...]
    """Все несоответствия разом, а не первое: сразу видно, чего драйверу не хватает."""

    def __init__(self, *, missing: tuple[str, ...]) -> None:
        if not missing:
            msg = "UnsupportedRequirementError без несоответствий: нечего сообщать"
            raise ValueError(msg)
        self.missing = missing
        super().__init__("Драйвер не умеет то, что требует запрос: " + "; ".join(missing))


class NoFreeEndpointError(PoolError):
    """Провайдер эндпоинтов не может дать браузер: все его адреса уже заняты слотами пула."""

    addresses: int
    """Сколько адресов у провайдера: браузеров в пуле должно быть не больше."""

    def __init__(self, *, addresses: int) -> None:
        self.addresses = addresses
        super().__init__(
            f"Все {addresses} адресов провайдера заняты: браузеров в пуле больше, чем адресов"
        )


class NoUsableProxyError(PoolError):
    """Identity нужен прокси, а источник не дал ни одного пригодного."""

    identity: str
    """Ключ identity, для которой не нашлось прокси."""

    def __init__(self, *, identity: str) -> None:
        self.identity = identity
        super().__init__(f"Нет пригодного прокси для {identity}")


class ProxyFailedError(PoolError):
    """Контекст не открылся из-за прокси - и попытки с другими прокси исчерпаны.

    Несёт сам прокси: выбирал его пул, и иначе вызывающий не узнал бы, какой пометить.
    В текст ошибки прокси попадает только безопасным именем, без кредов.
    """

    proxy: Proxy
    """Прокси, который не прошёл, - последний из опробованных."""
    identity: str
    """Ключ identity, для которой открывали контекст."""
    reason: str
    """Причина со стороны драйвера или источника."""

    def __init__(self, *, proxy: Proxy, identity: str, reason: str) -> None:
        self.proxy = proxy
        self.identity = identity
        self.reason = reason
        super().__init__(f"Прокси {proxy.label} не пропустил {identity}: {reason}")


class IdentityBusyError(PoolError):
    """Identity открыта в другом месте (другой пул, процесс, машина) и не освободилась вовремя."""

    identity: str
    """Ключ занятой identity."""

    def __init__(self, *, identity: str) -> None:
        self.identity = identity
        super().__init__(f"Identity {identity} занята в другом месте")


class IdentityCoolingDownError(PoolError):
    """Identity на паузе (`rate_limited`), а запрос был только на неё и ждать не велено."""

    identity: str
    """Ключ identity на паузе."""
    retry_after: float
    """Сколько секунд паузы осталось на момент отказа."""

    def __init__(self, *, identity: str, retry_after: float) -> None:
        if not math.isfinite(retry_after) or retry_after < 0:
            msg = f"retry_after должен быть конечным и неотрицательным, получено {retry_after}"
            raise ValueError(msg)
        self.identity = identity
        self.retry_after = retry_after
        super().__init__(f"{identity} на паузе ещё {retry_after:g} с")


class IdentityBlockedError(PoolError):
    """Identity заблокирована (бан, челлендж) и снимается только явно: `pool.unblock(key)`."""

    identity: str
    """Ключ заблокированной identity."""
    reason: str
    """За что заблокирована."""

    def __init__(self, *, identity: str, reason: str) -> None:
        self.identity = identity
        self.reason = reason
        super().__init__(f"{identity} заблокирована: {reason}")


__all__ = [
    "AcquireTimeoutError",
    "Classification",
    "Classifier",
    "ConfigError",
    "ErrorKind",
    "HasErrorKind",
    "IdentityBlockedError",
    "IdentityCoolingDownError",
    "LeaseRevokedError",
    "NoFreeEndpointError",
    "NoUsableProxyError",
    "PoolError",
    "PoolInvariantError",
    "PoolSaturatedError",
    "PoolSignal",
    "PoolStoppedError",
    "PoolUnavailableError",
    "ProxyFailedError",
    "StaleLeaseError",
    "StartupTimeoutError",
    "UnsupportedRequirementError",
    "declared_classification",
    "error_chain",
]
