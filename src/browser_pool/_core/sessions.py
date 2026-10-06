"""Сессии identity в пуле: восстановить, открыть, сохранить, закрыть.

Жизнь сессии привязана к физическому контексту:

1. **До создания контекста** — запись identity из хранилища: что восстановить. Сохранённое
   важнее `StatePolicy.initial`; состояние урезается под `state_support` драйвера. Состояние
   задаётся при создании контекста, а не дозаливается в живой.
2. **Открытие** — `flow.open` с временной вкладкой (закроется сама), под таймаутом `open` и
   лимитом одновременных открытий: вход — самая тяжёлая и самая заметная операция.
3. **Сразу после открытия — сохранение**: падение процесса не должно заставлять входить заново.
4. **Периодически и перед закрытием** — сохранение; снять состояние с умершего контекста — не
   ошибка, а событие.

Режимы (`StatePolicy.mode`): `read_write` — читать и сохранять, `read_only` — только читать,
`none` — хранилище не трогать. При конфликте версий (запись изменил кто-то ещё) держатель живого
контекста перечитывает версию и сохраняет снова: его состояние свежее — он только что был на сайте.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from browser_pool._core.threads import call_in_thread
from browser_pool.clock import monotonic
from browser_pool.events import SessionOpened, StateExportFailed, StateSaved, StateSaveFailed
from browser_pool.state import IdentityRecord, SessionState, StaleRecordError

if TYPE_CHECKING:
    from collections.abc import Callable

    from browser_pool.config import Limits, Timeouts
    from browser_pool.driver import Driver, StateSupport
    from browser_pool.events import PoolEvent
    from browser_pool.flow import SessionFlow
    from browser_pool.identity import Identity
    from browser_pool.proxies import Proxy
    from browser_pool.state import StateStore

_logger = logging.getLogger(__name__)


@dataclass(eq=False, slots=True)
class _Live:
    """Открытая сессия identity."""

    identity: Identity
    flow: SessionFlow[Any, Any, Any] | None
    session: object
    record: IdentityRecord | None
    last_saved: float
    proxy_ref: str | None = None
    """Прокси контекста — уходит в запись: по нему `sticky` найдёт тот же и после перезапуска."""


class _OpenRequest[C, P]:
    """То, что получает `flow.open`."""

    def __init__(
        self,
        sessions: Sessions[C, P],
        *,
        identity: Identity,
        context: C,
        restored: SessionState | None,
        proxy: Proxy | None,
    ) -> None:
        self._sessions = sessions
        self._identity = identity
        self._context = context
        self._restored = restored
        self._proxy = proxy
        self.pages: list[P] = []

    @property
    def identity(self) -> Identity:
        """Чья сессия."""
        return self._identity

    @property
    def context(self) -> C:
        """Нативный контекст."""
        return self._context

    @property
    def restored(self) -> SessionState | None:
        """Что восстановлено в контекст."""
        return self._restored

    @property
    def proxy(self) -> Proxy | None:
        """Прокси контекста."""
        return self._proxy

    async def new_page(self) -> P:
        """Временная вкладка; закроется после `open`."""
        page = await self._sessions.new_temporary_page(self._context)
        self.pages.append(page)
        return page

    async def save_state(self) -> None:
        """Сохранить состояние сейчас."""
        await self._sessions.save(self._identity, self._context, trigger="flow")

    async def call[**A, T](self, fn: Callable[A, T], /, *args: A.args, **kwargs: A.kwargs) -> T:
        """Sync-код входа в потоке браузера контекста."""
        return await call_in_thread(self._sessions.driver, self._context, fn, *args, **kwargs)


class Sessions[C, P]:
    """Сессии identity: хранилище, flow и снятие состояния с контекстов."""

    def __init__(
        self,
        driver: Driver[Any, C, P],
        *,
        state_support: StateSupport | None = None,
        store: StateStore,
        default_flow: SessionFlow[C, P, Any] | None,
        timeouts: Timeouts,
        limits: Limits,
        emit: Callable[[PoolEvent], None],
    ) -> None:
        self._driver = driver
        self._state_support: StateSupport = (
            state_support if state_support is not None else driver.capabilities.state_support
        )
        """Что из состояния пул сохраняет и восстанавливает: у профиля вендора — ничего."""
        self.driver: Driver[Any, C, P] = driver
        """Драйвер пула — для sync-кода flow в потоке браузера."""
        self._store = store
        self._default_flow = default_flow
        self._timeouts = timeouts
        self._emit = emit
        self._opens = limits.concurrent_opens
        self._opening = asyncio.Semaphore(limits.concurrent_opens)
        self._records: dict[str, IdentityRecord | None] = {}
        self._live: dict[str, _Live] = {}

    def reconfigure(self, limits: Limits) -> None:
        """Новый потолок одновременных входов — для следующих; идущие доработают."""
        if limits.concurrent_opens != self._opens:
            self._opens = limits.concurrent_opens
            self._opening = asyncio.Semaphore(limits.concurrent_opens)

    # --- до и во время открытия ----------------------------------------------------------

    async def restore(self, identity: Identity) -> SessionState | None:
        """Что восстановить в новый контекст identity — до его создания."""
        policy = identity.state
        record: IdentityRecord | None = None
        if policy.mode != "none":
            record = await self._store.load(identity.key)
        self._records[identity.key] = record
        state = record.state if record is not None and not record.state.is_empty() else None
        if state is None:
            state = policy.initial
        return self._fit(state)

    def pending_proxy_ref(self, key: str) -> str | None:
        """Прокси из прочитанной записи identity — после `restore`, до открытия."""
        return _proxy_ref(self._records.get(key))

    async def open(
        self,
        identity: Identity,
        context: C,
        *,
        restored: SessionState | None,
        proxy: Proxy | None,
        proxy_ref: str | None = None,
    ) -> None:
        """Открыть сессию в созданном контексте и сразу сохранить её состояние."""
        flow = identity.flow if identity.flow is not None else self._default_flow
        live = _Live(
            identity=identity,
            flow=flow,
            session=None,
            record=self._records.pop(identity.key, None),
            last_saved=monotonic(),
            proxy_ref=proxy_ref,
        )
        self._live[identity.key] = live
        if flow is None:
            if identity.proxy.mode == "sticky" and proxy_ref != _proxy_ref(live.record):
                # Закрепить прокси сразу.
                await self._save_quietly(identity, context, live, trigger="proxy")
            return
        opening = _OpenRequest(
            self, identity=identity, context=context, restored=restored, proxy=proxy
        )
        try:
            async with self._opening, asyncio.timeout(self._timeouts.open):
                live.session = await flow.open(opening)
        except BaseException:
            self._live.pop(identity.key, None)
            raise
        finally:
            for page in opening.pages:
                await self._close_quietly(page)
        self._emit(SessionOpened(key=identity.key, restored=restored is not None))
        try:
            # Вход прошёл: сбой хранилища — не повод ронять аренду и входить заново.
            await self._save_quietly(identity, context, live, trigger="open")
        except BaseException:
            # Отмена посреди сохранения: контекст закроют, открытую сессию закрываем сами.
            self._live.pop(identity.key, None)
            await self._close_flow(identity.key, live)
            raise

    async def new_temporary_page(self, context: C) -> P:
        """Вкладка для `flow.open`."""
        async with asyncio.timeout(self._timeouts.page_create):
            return await self._driver.new_page(context)

    # --- вкладки -------------------------------------------------------------------------

    async def prepare_page(self, key: str, page: P) -> None:
        """Прогрев новой вкладки flow identity."""
        live = self._live.get(key)
        if live is not None and live.flow is not None:
            await live.flow.prepare_page(live.session, page)

    async def reset_page(self, key: str, page: P) -> bool:
        """Сброс вкладки перед возвратом в запас."""
        live = self._live.get(key)
        if live is None or live.flow is None:
            return True
        return await live.flow.reset_page(live.session, page)

    def session_of(self, key: str) -> object:
        """Объект сессии, который вернул `flow.open`; `None` — flow нет."""
        live = self._live.get(key)
        return live.session if live is not None else None

    # --- сохранение и закрытие -----------------------------------------------------------

    async def save(self, identity: Identity, context: C, *, trigger: str) -> None:
        """Снять состояние с контекста и сохранить — если политика identity это позволяет."""
        await self._save(identity, context, self._live.get(identity.key), trigger=trigger)

    async def _save(
        self, identity: Identity, context: C, live: _Live | None, *, trigger: str
    ) -> None:
        # live передаётся явно: при закрытии сессия уже снята с учёта, а её запись нужна.
        if identity.state.mode != "read_write" or self._state_support == "none":
            return
        state = await self.export(identity.key, context)
        if state is None:
            return
        base = (
            live.record
            if live is not None and live.record is not None
            else IdentityRecord(key=identity.key)
        )
        record = replace(base, state=state)
        if live is not None:
            record = replace(record, proxy_ref=live.proxy_ref)
        async with asyncio.timeout(self._timeouts.state_export):
            try:
                saved = await self._store.save(record)
            except StaleRecordError as conflict:
                _logger.warning(
                    "Запись %s изменил кто-то ещё (версия %d); сохраняю поверх свежее состояние контекста",
                    identity.key,
                    conflict.actual,
                )
                saved = await self._store.save(replace(record, version=conflict.actual))
        if live is not None:
            live.record = saved
            live.last_saved = monotonic()
        self._emit(StateSaved(key=identity.key, version=saved.version, trigger=trigger))

    async def _save_quietly(
        self, identity: Identity, context: C, live: _Live | None, *, trigger: str
    ) -> None:
        """Сохранить состояние; сбой хранилища — запись в лог и событие, а не исключение."""
        try:
            await self._save(identity, context, live, trigger=trigger)
        except Exception as error:
            _logger.warning(
                "Состояние %s не сохранилось (%s)", identity.key, trigger, exc_info=True
            )
            self._emit(
                StateSaveFailed(key=identity.key, error=type(error).__name__, trigger=trigger)
            )

    async def export(self, key: str, context: C) -> SessionState | None:
        """Состояние живого контекста; умер — `None` и событие, а не ошибка."""
        try:
            async with asyncio.timeout(self._timeouts.state_export):
                return await self._driver.export_state(context)
        except Exception as error:  # noqa: BLE001 — мёртвый контекст: снимать нечего, это не сбой пула
            _logger.info("Состояние %s не снялось: %s", key, type(error).__name__)
            self._emit(StateExportFailed(key=key, error=type(error).__name__))
            return None

    async def save_due(self, contexts: dict[str, C], *, interval: float) -> None:
        """Сохранить состояние контекстов, с последнего сохранения которых прошло `interval`."""
        now = monotonic()
        for key, context in contexts.items():
            live = self._live.get(key)
            if live is not None and now - live.last_saved >= interval:
                # Сбой хранилища одной identity не мешает ни остальным, ни проверке здоровья.
                await self._save_quietly(live.identity, context, live, trigger="interval")
                live.last_saved = (
                    monotonic()
                )  # следующая попытка — через интервал, а не на каждой проверке

    async def close(self, key: str, context: C) -> None:
        """Контекст закрывается: сохранить состояние, закрыть сессию flow. Не бросает."""
        live = self._live.pop(key, None)
        if live is None:
            return
        await self._save_quietly(live.identity, context, live, trigger="close")
        await self._close_flow(key, live)

    async def _close_flow(self, key: str, live: _Live) -> None:
        """`flow.close` открытой сессии. Не бросает."""
        if live.flow is None:
            return
        try:
            async with asyncio.timeout(self._timeouts.close):
                await live.flow.close(live.session)
        except Exception:
            _logger.warning("flow.close для %s упал", key, exc_info=True)

    def forget_pending(self, key: str) -> None:
        """Контекст не создался: запись, прочитанную для него, держать незачем."""
        self._records.pop(key, None)

    # --- внутреннее --------------------------------------------------------------------

    def _fit(self, state: SessionState | None) -> SessionState | None:
        """Урезать состояние под то, что драйвер умеет восстановить."""
        support = self._state_support
        if state is None or support == "none":
            return None
        if support == "cookies":
            return SessionState(cookies=state.cookies, extras=state.extras)
        return state

    async def _close_quietly(self, page: P) -> None:
        try:
            async with asyncio.timeout(self._timeouts.close):
                await self._driver.close_page(page)
        except Exception:
            _logger.warning("Временная вкладка входа не закрылась", exc_info=True)


def _proxy_ref(record: IdentityRecord | None) -> str | None:
    return record.proxy_ref if record is not None else None


__all__ = ["Sessions"]
