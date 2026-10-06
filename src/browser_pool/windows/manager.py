"""Менеджер окон: когда и кого размещать — поверх `LayoutEngine` и `WindowControl` драйвера.

Только для отладки в headed-режиме. Менеджер подключается к пулу хуками и событиями:

- новая вкладка (`after_page_created`) — окно получает ячейку: окно на аккаунт (`per_context`, окно первой
  вкладки контекста) или на каждую вкладку (`per_page`);
- аренда (`after_acquire`/`before_release`) — учёт, какие окна заняты: свёрнутое окно, которое снова
  понадобилось, разворачивается в ячейке самого давно простаивающего соседа (`minimize_idle`);
- закрытие контекста — окна освобождают ячейки; свёрнутые из-за нехватки места их получают;
- закрылась вкладка, по которой окно нашли, — окно аккаунта держит любая другая его живая вкладка,
  ячейка остаётся; окно без живых вкладок ячейку отдаёт.

При `reflow="stable"` живое окно не двигается никогда; окно, которое человек передвинул руками,
остаётся на месте до `retile()` (`respect_manual`). Драйвер, который двигает окна только при
запуске (`launch_only`), получает прямоугольник ячейки в `LaunchSpec.window` — ячейка на браузер.

Сбой любой оконной операции — запись в лог, не исключение: отладочная функция не роняет аренду.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from browser_pool.driver import WindowBounds, WindowControl, WindowState
from browser_pool.events import WindowPlaced
from browser_pool.geometry import Rect
from browser_pool.windows.layout import LayoutEngine, LayoutPolicy
from browser_pool.windows.screen import explicit_areas, resolve_areas

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from browser_pool.config import Windows
    from browser_pool.driver import Driver, LaunchSpec, WindowId
    from browser_pool.events import PoolEvent
    from browser_pool.identity import Identity
    from browser_pool.lease import PageLease

_logger = logging.getLogger(__name__)

_MANUAL_TOLERANCE = 16
"""На сколько пикселей окно может отличаться от поставленного, не считаясь сдвинутым руками: ОС и Chrome поправляют границы."""


def display_available() -> bool:
    """Есть ли у процесса экран: на Linux без `DISPLAY` и `WAYLAND_DISPLAY` окно показать негде."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _area(areas: list[Rect]) -> int:
    return sum(rect.width * rect.height for rect in areas)


def _moved(current: Rect, applied: Rect) -> bool:
    """Окно сдвинуто руками: границы разошлись с поставленными больше допуска."""
    return any(
        abs(left - right) > _MANUAL_TOLERANCE
        for left, right in (
            (current.x, applied.x),
            (current.y, applied.y),
            (current.width, applied.width),
            (current.height, applied.height),
        )
    )


@dataclass(eq=False, slots=True)
class _Window[B, P]:
    key: str
    identity: str
    browser_id: str
    browser: B
    window: WindowId
    page: P
    """Якорная вкладка окна. Закрылась — окно держит другая живая вкладка из `pages`."""
    seq: int
    applied: Rect | None = None
    """Куда менеджер поставил окно в последний раз."""
    folded: bool = False
    """Свёрнуто `minimize_idle()`: развернётся при следующей аренде."""
    parked: bool = False
    """Свёрнуто, потому что ячеек не хватило: развернётся, как только ячейка освободится."""
    manual: bool = False
    """Человек передвинул окно — не трогать до `retile()`."""
    active: int = 0
    last_used: int = 0
    extras: list[WindowId] = field(default_factory=list["WindowId"])
    """Окна других вкладок того же аккаунта (`per_context`, SDK открывает вкладку окном) — в той же ячейке."""
    pages: list[P] = field(default_factory=list["P"])
    """Вкладки, открывшиеся в этом окне и его спутниках, включая якорную; закрытые вычищает `_prune`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowView:
    """Окно глазами менеджера — для `pool.windows.snapshot()`."""

    key: str
    identity: str
    browser_id: str
    slot: int | None
    rect: Rect | None
    folded: bool
    """Свёрнуто: простаивает (`minimize_idle`) или не хватило ячейки."""
    manual: bool
    active: int


class WindowManager[B, P]:
    """Раскладка окон пула. Выключен — все методы ничего не делают."""

    def __init__(
        self,
        driver: Driver[B, Any, P],
        *,
        config: Windows,
        locate: Callable[[str], tuple[str, B] | None],
        emit: Callable[[PoolEvent], None],
        timeout: float,
        tabs_per_identity: int = 1,
    ) -> None:
        """`locate(key)` — браузер identity: его номер в пуле и нативный объект.

        `tabs_per_identity` — сколько вкладок может быть у аккаунта: размер его блока ячеек при
        `group="identity"` (в окне на аккаунт — один).
        """
        self._driver = driver
        self._config = config
        self._control = (
            cast("WindowControl[B, P]", driver) if isinstance(driver, WindowControl) else None
        )
        self._level = driver.capabilities.window_control
        self._per_page = config.mode == "per_page" and driver.capabilities.new_window
        self._grouped = config.group == "identity" and config.layout == "grid"
        self._block = max(1, tabs_per_identity) if self._per_page else 1
        self._blocks: dict[str, int] = {}
        """Блок ячеек аккаунта (`group="identity"`): номер блока по ключу identity."""
        self._locate = locate
        self._emit = emit
        self._timeout = timeout
        self._engine = LayoutEngine(
            LayoutPolicy(
                layout=config.layout,
                reflow=config.reflow,
                size=config.size,
                min_size=config.min_size,
                gap=config.gap,
                margin=config.margin,
                max_windows=config.max_windows,
                columns=self._block if self._grouped else None,
            )
        )
        self._windows: dict[str, _Window[B, P]] = {}
        self._slots: dict[str, int] = {}
        self._launch_slots: dict[str, int] = {}
        self._areas: list[Rect] | None = explicit_areas(config.screen)
        self._lock = asyncio.Lock()
        self._seq = itertools.count(1)
        self._suppressed = self._why_not_shown(driver)

    def _why_not_shown(self, driver: Driver[B, Any, P]) -> str | None:
        """Секция включена, а показать окна негде: драйвер headless или у процесса нет экрана."""
        if not self._config.active:
            return None
        if getattr(driver, "headless", None) is True:
            return "драйвер запущен headless (headless=True): окон нет, двигать нечего"
        if not display_available():
            return "у процесса нет экрана (DISPLAY / WAYLAND_DISPLAY): окну негде показаться"
        return None

    @property
    def shown(self) -> bool:
        """Браузеры запускаются с окнами (headed): секция включена и показать их есть где."""
        return self._config.active and self._suppressed is None

    @property
    def enabled(self) -> bool:
        """Окна раскладываются на лету."""
        return self.shown and self._control is not None and self._level == "runtime"

    @property
    def at_launch(self) -> bool:
        """Драйвер ставит окно только при запуске: ячейка уходит в `LaunchSpec.window`."""
        return self.shown and self._level == "launch_only"

    def problem(self) -> str | None:
        """Почему раскладка включена в конфиге, но работать не будет; `None` — всё в порядке."""
        if not self._config.active:
            return None
        if self._suppressed is not None:
            return self._suppressed
        if self.enabled or self.at_launch:
            return None
        if self._control is None or self._level == "none":
            return "драйвер не умеет двигать окна (window_control='none')"
        return "драйвер не реализует WindowControl"

    def downgrade(self) -> str | None:
        """Раскладка работает, но не так, как просили в конфиге; `None` — как просили."""
        if self.enabled and self._config.mode == "per_page" and not self._per_page:
            return (
                "mode='per_page' недоступен: драйвер не открывает вкладку отдельным окном "
                "(new_window=False) — окно на аккаунт, как per_context"
            )
        return None

    # --- хуки пула ---------------------------------------------------------------------

    async def page_opened(self, page: P, identity: Identity) -> None:
        """Новая вкладка — её окну нужна ячейка."""
        if self.enabled:
            await self._quietly("размещение окна", lambda: self._open(page, identity))

    async def lease_acquired(self, lease: PageLease[B, Any, P, Any]) -> None:
        """Окно арендованной вкладки — занято; свёрнутое разворачивается, если есть место."""
        if self.enabled:
            await self._quietly("выдача окна", lambda: self._touch(lease, delta=1))

    async def lease_released(self, lease: PageLease[B, Any, P, Any]) -> None:
        """Окно вернувшейся вкладки простаивает."""
        if self.enabled:
            await self._quietly("возврат окна", lambda: self._touch(lease, delta=-1))

    async def context_closed(self, key: str) -> None:
        """Контекст закрыт: его окна освобождают ячейки."""
        if self.enabled:
            await self._quietly("закрытие окна", lambda: self._drop(key))

    async def before_launch(self, spec: LaunchSpec, browser_id: str) -> None:
        """`launch_only`: ячейка браузера — в спецификацию запуска."""
        if not self.at_launch:
            return
        if self._areas is None:
            _logger.warning("Окна: драйвер ставит окно только при запуске — задайте Windows.screen")
            return
        keys = [
            *self._launch_slots,
            *([browser_id] if browser_id not in self._launch_slots else []),
        ]
        plan = self._engine.plan(self._areas, keys, previous=self._launch_slots)
        self._launch_slots = dict(plan.slots)
        spec.window = plan.rects.get(browser_id)

    # --- управление ------------------------------------------------------------------------

    async def retile(self) -> None:
        """Вернуть все окна в сетку — и те, что двигали руками."""
        if not self.enabled:
            return
        for window in self._windows.values():
            window.manual = False
            window.folded = False
            window.applied = None
        await self._quietly("перераскладка", self._relayout)

    async def focus(self, key: str) -> bool:
        """Поднять окно identity (или вкладки) наверх; свёрнутое — развернуть. `False` — окна нет."""
        window = self._windows.get(key)
        if not self.enabled or window is None or self._control is None:
            return False
        control = self._control
        window.last_used = next(self._seq)
        window.folded = False
        window.active += 1  # на время раскладки окно «нужно», чтобы получить ячейку
        try:
            await self._quietly("перераскладка", self._relayout)
        finally:
            window.active -= 1
        await self._quietly("фокус окна", lambda: control.bring_to_front(window.page))
        return True

    async def minimize_idle(self) -> int:
        """Свернуть окна, у которых нет аренд; развернутся при следующей аренде. Сколько свёрнуто."""
        if not self.enabled:
            return 0
        idle = [
            window
            for window in self._windows.values()
            if not window.active and not window.folded and not window.parked
        ]
        for window in idle:
            await self._quietly("сворачивание окна", self._folder(window))
        return len(idle)

    def _folder(self, window: _Window[B, P]) -> Callable[[], Awaitable[None]]:
        async def fold() -> None:
            await self._fold(window)
            window.folded = True

        return fold

    def snapshot(self) -> tuple[WindowView, ...]:
        """Какое окно в какой ячейке, чьё и занято ли."""
        return tuple(
            WindowView(
                key=window.key,
                identity=window.identity,
                browser_id=window.browser_id,
                slot=self._slots.get(window.key),
                rect=window.applied,
                folded=window.folded or window.parked,
                manual=window.manual,
                active=window.active,
            )
            for window in self._windows.values()
        )

    # --- внутреннее: учёт ------------------------------------------------------------------

    async def _open(self, page: P, identity: Identity) -> None:
        located = self._locate(identity.key)
        if located is None or self._control is None:
            return
        browser_id, browser = located
        window = await self._control.window_of(page)
        key = f"{identity.key}#{window}" if self._per_page else identity.key
        async with self._lock:
            owner = self._windows.get(key)
            if owner is not None:
                owner.pages.append(page)  # до чистки: все прежние закрылись — окно держит эта
            await self._prune()
            if key in self._windows:
                owner = self._windows[key]
                await self._join(owner, window)
                if owner.applied is None and not owner.folded and not owner.parked:
                    await (
                        self._relayout_locked()
                    )  # якорем стала вкладка в новом окне: его тоже в ячейку
                return
            await self._learn_screen(page)
            self._windows[key] = _Window(
                key=key,
                identity=identity.key,
                browser_id=browser_id,
                browser=browser,
                window=window,
                page=page,
                seq=next(self._seq),
                last_used=next(self._seq),
                active=1,  # вкладку открыли под аренду: окно нужно сейчас
                pages=[page],
            )
            try:
                await self._relayout_locked()
            finally:
                self._windows[
                    key
                ].active = 0  # счёт аренд ведут хуки аренды; сбой размещения не залипает

    async def _learn_screen(self, page: P) -> None:
        """`screen="auto"`: область — по первой вкладке; большую увидела другая вкладка — берём её.

        Вкладка с эмуляцией viewport видит «экран» размером с viewport; запоминать такую область
        навсегда значило бы оставить пулу два окна.
        """
        if self._control is None:
            return
        if explicit_areas(self._config.screen) is not None and self._areas is not None:
            return
        seen = await resolve_areas(self._config.screen, control=self._control, page=page)
        if self._areas is None or _area(seen) > _area(self._areas):
            grown = self._areas is not None
            self._areas = seen
            if grown:
                for window in self._windows.values():
                    window.applied = None  # ячейки пересчитаются под большую область

    async def _join(self, owner: _Window[B, P], window: WindowId) -> None:
        """Ещё одна вкладка аккаунта открылась своим окном: поставить его в ячейку аккаунта."""
        if window == owner.window or window in owner.extras or self._control is None:
            return
        owner.extras.append(window)
        if owner.applied is not None and not owner.folded and not owner.parked:
            await self._control.set_bounds(owner.browser, window, WindowBounds(rect=owner.applied))

    async def _follow(self, owner: _Window[B, P], bounds: WindowBounds) -> None:
        """Окна других вкладок аккаунта — туда же; закрытые выпадают из списка."""
        if self._control is None:
            return
        for extra in tuple(owner.extras):
            try:
                await self._control.set_bounds(owner.browser, extra, bounds)
            except Exception:  # noqa: BLE001 — окно закрыто: забыть его
                owner.extras.remove(extra)

    async def _touch(self, lease: PageLease[B, Any, P, Any], *, delta: int) -> None:
        window = self._window_of_page(lease.page) or self._windows.get(lease.identity.key)
        if window is None:
            return
        window.active = max(0, window.active + delta)
        window.last_used = next(self._seq)
        if delta > 0:
            if window.folded or window.parked:
                window.folded = False
                await self._relayout()
            if self._config.focus_on_acquire and self._control is not None:
                await self._control.bring_to_front(lease.page)

    async def _drop(self, identity: str) -> None:
        async with self._lock:
            gone = [key for key, window in self._windows.items() if window.identity == identity]
            for key in gone:
                del self._windows[key]
                self._slots.pop(key, None)
            if gone:
                await self._relayout_locked()

    async def _prune(self) -> None:
        """Окно без живых вкладок не держит ячейку; окно с закрытой якорной вкладкой переходит на живую."""
        for key, window in tuple(self._windows.items()):
            if not await self._reanchor(window):
                del self._windows[key]
                self._slots.pop(key, None)

    async def _reanchor(self, window: _Window[B, P]) -> bool:
        """Якорная вкладка закрылась — якорем становится другая живая. `False` — живых нет."""
        window.pages = [page for page in window.pages if self._driver.page_usable(page)]
        if self._driver.page_usable(window.page):
            return True
        if not window.pages or self._control is None:
            return False
        window.page = window.pages[0]
        native = await self._control.window_of(window.page)
        if native != window.window:  # вкладка жила в другом окне: теперь оно и есть окно аккаунта
            window.window = native
            window.applied = None  # новое окно ещё не в ячейке
            if native in window.extras:
                window.extras.remove(native)
        return True

    def _window_of_page(self, page: P) -> _Window[B, P] | None:
        return next(
            (
                window
                for window in self._windows.values()
                if any(known is page for known in window.pages)
            ),
            None,
        )

    # --- внутреннее: раскладка -------------------------------------------------------------

    async def _relayout(self) -> None:
        async with self._lock:
            await self._prune()
            await self._relayout_locked()

    async def _relayout_locked(self) -> None:
        if self._areas is None or not self._windows:
            return
        order = self._ordered()
        if self._grouped:
            placed = self._group_slots(order)
            plan = self._engine.plan(self._areas, placed, previous=self._slots)
        else:
            self._make_room(order)
            plan = self._engine.plan(self._areas, order, previous=self._slots)
        self._slots = dict(plan.slots)
        for key in order:
            window = self._windows[key]
            target = plan.rects.get(key)
            if target is None:
                await self._overflow(window)
            elif not window.folded:
                window.parked = False
                await self._place(window, target, slot=plan.slots[key])

    def _ordered(self) -> list[str]:
        """Порядок окон: нужные сейчас без ячейки — первыми, чтобы свободная ячейка досталась им."""
        keys = sorted(self._windows, key=self._order_key)
        wanted = [key for key in keys if key not in self._slots and self._windows[key].active]
        return wanted + [key for key in keys if key not in wanted]

    def _order_key(self, key: str) -> tuple[str, int]:
        window = self._windows[key]
        match self._config.order:
            case "identity":
                return (window.key, window.seq)
            case "browser":
                return (window.browser_id, window.seq)
            case "created":
                return ("", window.seq)

    def _group_slots(self, order: list[str]) -> list[str]:
        """`group="identity"`: окнам — ячейки в блоке своего аккаунта. Возвращает окна с ячейкой.

        Блок закреплён за аккаунтом, пока у него есть окна; свободный — наименьший по номеру. Окно,
        которому места в блоке нет (или блока нет), в раскладку не идёт — его свернёт `overflow`.
        """
        assert self._areas is not None  # noqa: S101 — зовётся из раскладки с известной областью
        blocks = self._engine.capacity(self._areas) // self._block
        owners = {self._windows[key].identity for key in order}
        self._blocks = {key: block for key, block in self._blocks.items() if key in owners}
        taken = {slot for key, slot in self._slots.items() if key in self._windows}
        placed = [key for key in order if key in self._slots]
        for key in order:
            if key in self._slots:
                continue
            block = self._block_of(self._windows[key].identity, blocks)
            slot = self._free_cell(block, taken) if block is not None else None
            if slot is not None:
                self._slots[key] = slot
                taken.add(slot)
                placed.append(key)
        return placed

    def _block_of(self, identity: str, blocks: int) -> int | None:
        """Блок аккаунта: закреплённый или наименьший свободный; свободных нет — `None`."""
        block = self._blocks.get(identity)
        if block is None:
            used = set(self._blocks.values())
            block = next((index for index in range(blocks) if index not in used), None)
            if block is not None:
                self._blocks[identity] = block
        return block

    def _free_cell(self, block: int, taken: set[int]) -> int | None:
        start = block * self._block
        return next((cell for cell in range(start, start + self._block) if cell not in taken), None)

    def _make_room(self, order: list[str]) -> None:
        """Ячеек нет, а окно нужно: забрать ячейку у самого давно простаивающего (`minimize_idle`)."""
        if self._config.overflow != "minimize_idle" or self._areas is None:
            return
        capacity = self._engine.capacity(self._areas)
        needy = [key for key in order if key not in self._slots and self._windows[key].active]
        free = capacity - len(self._slots)
        idle = sorted(
            (
                window
                for window in self._windows.values()
                if window.key in self._slots and not window.active
            ),
            key=lambda window: window.last_used,
        )
        for victim in idle[: max(0, len(needy) - free)]:
            del self._slots[victim.key]
            victim.applied = None

    async def _place(self, window: _Window[B, P], target: Rect, *, slot: int) -> None:
        if window.applied == target or window.manual or self._control is None:
            return
        control = self._control
        if window.applied is not None and self._config.respect_manual:
            current = await control.get_bounds(window.browser, window.window)
            if current.state is WindowState.normal and _moved(current.rect, window.applied):
                window.manual = True  # человек передвинул — не спорим до retile()
                return
        rect = target
        if not self._config.snap or self._config.layout == "free":
            current = await control.get_bounds(window.browser, window.window)
            rect = Rect(
                x=current.rect.x, y=current.rect.y, width=target.width, height=target.height
            )
        await control.set_bounds(window.browser, window.window, WindowBounds(rect=rect))
        window.applied = rect
        await self._follow(window, WindowBounds(rect=rect))
        self._placed(window, rect, slot=slot)

    async def _overflow(self, window: _Window[B, P]) -> None:
        """Окну не хватило ячейки: свернуть, лесенкой или оставить как есть (`tabs`)."""
        match self._config.overflow:
            case "minimize_idle":
                if window.active:
                    return  # окно в работе не сворачивают: пусть без ячейки, но на виду
                if not window.parked:
                    await self._fold(window)
                    window.parked = True
            case "cascade":
                await self._cascade(window)
            case "tabs":
                pass

    async def _fold(self, window: _Window[B, P]) -> None:
        if self._control is None:
            return
        current = await self._control.get_bounds(window.browser, window.window)
        minimized = WindowBounds(rect=current.rect, state=WindowState.minimized)
        await self._control.set_bounds(window.browser, window.window, minimized)
        await self._follow(window, minimized)
        window.applied = None
        self._placed(window, current.rect, slot=None)

    async def _cascade(self, window: _Window[B, P]) -> None:
        if self._control is None or self._areas is None:
            return
        overflowed = [key for key in self._windows if key not in self._slots]
        step = LayoutEngine(LayoutPolicy(layout="cascade", min_size=self._config.min_size))
        rect = step.plan(self._areas[:1], overflowed).rects.get(window.key)
        if rect is None or rect == window.applied:
            return
        await self._control.set_bounds(window.browser, window.window, WindowBounds(rect=rect))
        window.applied = rect
        self._placed(window, rect, slot=None)

    def _placed(self, window: _Window[B, P], rect: Rect, *, slot: int | None) -> None:
        self._emit(
            WindowPlaced(
                key=window.key,
                slot=slot,
                x=rect.x,
                y=rect.y,
                width=rect.width,
                height=rect.height,
            )
        )

    async def _quietly(self, what: str, operation: Callable[[], Awaitable[object]]) -> None:
        """Оконная операция не роняет пул: сбой — в лог."""
        try:
            async with asyncio.timeout(self._timeout):
                await operation()
        except Exception as error:  # noqa: BLE001 — отладочная функция не роняет аренду
            _logger.warning("Окна: %s не удалось (%s)", what, type(error).__name__)


__all__ = ["WindowManager", "WindowView"]
