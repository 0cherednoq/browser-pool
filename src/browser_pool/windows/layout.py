"""Раскладка окон для отладки: чистая функция, без I/O и SDK.

`LayoutEngine.plan(areas, windows, previous)` отвечает на вопрос «где стоять каждому окну»:

- `areas` — рабочие области мониторов по порядку; ячейки заполняют сначала первый, потом следующий;
- `windows` — ключи окон в нужном порядке (порядок выбирает `WindowManager`);
- `previous` — ячейки, которые окна занимали раньше: при `reflow="stable"` окно живёт в своей
  ячейке всю жизнь, освободившуюся занимает следующее новое окно, соседи не прыгают;
  при `"fill"` сетка каждый раз считается заново на текущее число окон.

Окна, которым не хватило ячеек, возвращаются в `overflow`: что с ними делать (свернуть, лесенкой,
вкладками), решает менеджер.

Раскладки: `grid` — сетка, пропорции ячейки ближе всего к пропорциям области (а не «всё в одну
строку»); `columns` / `rows` — в ряд; `cascade` — лесенкой с перекрытием; `free` — только размер
(позицию менеджер не трогает). Для `grid`, `columns` и `rows` ячейки не пересекаются, отстоят
друг от друга не меньше чем на `gap`, лежат внутри области с отступом `margin` и не меньше
`min_size`. Результат детерминирован.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from browser_pool.geometry import Rect

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type LayoutKind = Literal["grid", "columns", "rows", "cascade", "free"]
type Reflow = Literal["stable", "fill"]

_LAYOUTS = frozenset({"grid", "columns", "rows", "cascade", "free"})
_REFLOWS = frozenset({"stable", "fill"})
_CASCADE_STEP = 32
_CASCADE_SHARE = 0.6
"""Окно лесенкой — такая доля области, если размер не задан."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LayoutPolicy:
    """Как раскладывать окна."""

    layout: LayoutKind = "grid"
    reflow: Reflow = "stable"
    size: tuple[int, int] | None = None
    """Фиксированный размер окна; `None` — размер ячейки."""
    min_size: tuple[int, int] = (560, 400)
    """Меньше ячейку не делать: не помещается — окно уходит в `overflow`."""
    gap: int = 8
    """Зазор между окнами, px."""
    margin: int = 0
    """Отступ от краёв рабочей области, px."""
    max_windows: int | None = None
    """Потолок ячеек; `None` — сколько помещается при `min_size`."""
    columns: int | None = None
    """Столбцов в сетке ровно столько (если помещаются); `None` — по пропорциям области."""

    def __post_init__(self) -> None:
        problems: list[str] = []
        if self.layout not in _LAYOUTS:
            problems.append(f"layout {self.layout!r}: ждём {', '.join(sorted(_LAYOUTS))}")
        if self.reflow not in _REFLOWS:
            problems.append(f"reflow {self.reflow!r}: ждём {', '.join(sorted(_REFLOWS))}")
        if min(self.min_size) < 1:
            problems.append(f"min_size должен быть положительным: {self.min_size}")
        if self.size is not None and (
            self.size[0] < self.min_size[0] or self.size[1] < self.min_size[1]
        ):
            problems.append(f"size {self.size} меньше min_size {self.min_size}")
        if self.gap < 0 or self.margin < 0:
            problems.append(f"gap и margin не могут быть отрицательными: {self.gap}, {self.margin}")
        if self.columns is not None and self.columns < 1:
            problems.append(f"columns должен быть ≥ 1: {self.columns}")
        if self.max_windows is not None and self.max_windows < 1:
            problems.append(f"max_windows должен быть ≥ 1: {self.max_windows}")
        if problems:
            raise ValueError("; ".join(problems))


@dataclass(frozen=True, slots=True)
class Plan:
    """Где стоять окнам: ячейка и её номер у каждого размещённого, остальные — в `overflow`."""

    rects: Mapping[str, Rect] = field(default_factory=lambda: MappingProxyType({}))
    slots: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    """Номер ячейки окна — передаётся следующему `plan` как `previous`."""
    overflow: tuple[str, ...] = ()


class LayoutEngine:
    """Чистая раскладка окон по рабочим областям."""

    def __init__(self, policy: LayoutPolicy | None = None) -> None:
        """Политика раскладки; по умолчанию — сетка со стабильными ячейками."""
        self.policy: LayoutPolicy = policy if policy is not None else LayoutPolicy()

    def capacity(self, areas: Sequence[Rect]) -> int:
        """Сколько окон помещается при `min_size` (с учётом `max_windows`)."""
        total = sum(self._area_capacity(area) for area in areas)
        if self.policy.max_windows is not None:
            total = min(total, self.policy.max_windows)
        return total

    def plan(
        self,
        areas: Sequence[Rect],
        windows: Sequence[str],
        previous: Mapping[str, int] | None = None,
    ) -> Plan:
        """Раскладка `windows` (в этом порядке) по `areas`; `previous` — прошлые ячейки окон."""
        if len(set(windows)) != len(windows):
            msg = "окна в раскладке повторяются"
            raise ValueError(msg)
        capacity = self.capacity(areas)
        if self.policy.layout == "cascade":
            return self._cascade(areas, windows)
        if self.policy.layout == "free":
            return self._free(areas, windows)
        if self.policy.reflow == "fill":
            placed = list(windows[:capacity])
            cells = self._cells(areas, len(placed))
            slots = {key: index for index, key in enumerate(placed)}
        else:
            cells = self._cells(areas, capacity)
            slots = _stable_slots(windows, previous or {}, capacity)
        return Plan(
            rects=MappingProxyType({key: cells[slot] for key, slot in slots.items()}),
            slots=MappingProxyType(slots),
            overflow=tuple(key for key in windows if key not in slots),
        )

    def _free(self, areas: Sequence[Rect], windows: Sequence[str]) -> Plan:
        """`free`: менеджер задаёт только размер, позицию не трогает — размер получают все окна."""
        inner = _inset(areas[0], self.policy.margin) if areas else None
        if inner is None:
            return Plan(overflow=tuple(windows))
        width, height = self.policy.size or self.policy.min_size
        cell = Rect(
            x=inner.x, y=inner.y, width=min(width, inner.width), height=min(height, inner.height)
        )
        return Plan(
            rects=MappingProxyType(dict.fromkeys(windows, cell)),
            slots=MappingProxyType({key: index for index, key in enumerate(windows)}),
        )

    # --- ячейки ------------------------------------------------------------------------

    def _cells(self, areas: Sequence[Rect], count: int) -> list[Rect]:
        """`count` ячеек: области заполняются по порядку, каждая — не больше своей ёмкости."""
        cells: list[Rect] = []
        for area in areas:
            if len(cells) >= count:
                break
            take = min(self._area_capacity(area), count - len(cells))
            if take:
                cells.extend(self._area_cells(area, take))
        return cells

    def _area_capacity(self, area: Rect) -> int:
        inner = _inset(area, self.policy.margin)
        if inner is None:
            return 0
        width, height = self._cell_size(inner)
        columns = _fit(inner.width, width, self.policy.gap)
        rows = _fit(inner.height, height, self.policy.gap)
        match self.policy.layout:
            case "columns":
                return columns if rows else 0
            case "rows":
                return rows if columns else 0
            case _ if self.policy.columns is not None:
                return min(columns, self.policy.columns) * rows
            case _:
                return columns * rows

    def _cell_size(self, inner: Rect) -> tuple[int, int]:
        """Размер ячейки: `size` (не больше самой области — `size` больше экрана даёт одну ячейку) или `min_size`."""
        if self.policy.size is None:
            return self.policy.min_size
        width, height = self.policy.size
        return min(width, inner.width), min(height, inner.height)

    def _area_cells(self, area: Rect, count: int) -> list[Rect]:
        inner = _inset(area, self.policy.margin)
        if inner is None:  # pragma: no cover — ёмкость такой области 0, сюда не доходит
            return []
        columns, rows = self._shape(inner, count)
        gap = self.policy.gap
        if self.policy.size is not None:
            width, height = self._cell_size(inner)
        else:
            width = (inner.width - (columns - 1) * gap) // columns
            height = (inner.height - (rows - 1) * gap) // rows
        return [
            Rect(
                x=inner.x + (index % columns) * (width + gap),
                y=inner.y + (index // columns) * (height + gap),
                width=width,
                height=height,
            )
            for index in range(count)
        ]

    def _shape(self, inner: Rect, count: int) -> tuple[int, int]:
        """Столбцы и строки для `count` ячеек."""
        gap = self.policy.gap
        min_width, min_height = self._cell_size(inner)
        max_columns = _fit(inner.width, min_width, gap)
        max_rows = _fit(inner.height, min_height, gap)
        match self.policy.layout:
            case "columns":
                return count, 1
            case "rows":
                return 1, count
            case _ if self.policy.size is not None or self.policy.columns is not None:
                wanted = self.policy.columns if self.policy.columns is not None else count
                columns = max(1, min(max_columns, wanted, count))
                return columns, math.ceil(count / columns)
            case _:
                return _best_grid(inner, count, max_columns=max_columns, max_rows=max_rows, gap=gap)

    def _cascade(self, areas: Sequence[Rect], windows: Sequence[str]) -> Plan:
        """Лесенкой по первой области: окна перекрываются, но все видны заголовками."""
        inner = _inset(areas[0], self.policy.margin) if areas else None
        if inner is None:
            return Plan(overflow=tuple(windows))
        width, height = self.policy.size or (
            max(self.policy.min_size[0], int(inner.width * _CASCADE_SHARE)),
            max(self.policy.min_size[1], int(inner.height * _CASCADE_SHARE)),
        )
        width, height = min(width, inner.width), min(height, inner.height)
        steps = max(
            1,
            min(inner.width - width, inner.height - height) // _CASCADE_STEP + 1,
        )
        placed = windows[: self.policy.max_windows] if self.policy.max_windows else windows
        rects = {
            key: Rect(
                x=inner.x + (index % steps) * _CASCADE_STEP,
                y=inner.y + (index % steps) * _CASCADE_STEP,
                width=width,
                height=height,
            )
            for index, key in enumerate(placed)
        }
        return Plan(
            rects=MappingProxyType(rects),
            slots=MappingProxyType({key: index for index, key in enumerate(placed)}),
            overflow=tuple(windows[len(placed) :]),
        )


def _stable_slots(
    windows: Sequence[str], previous: Mapping[str, int], capacity: int
) -> dict[str, int]:
    """Живые окна остаются в своих ячейках; новые занимают свободные по порядку."""
    slots: dict[str, int] = {}
    taken: set[int] = set()
    for key in windows:
        slot = previous.get(key)
        if slot is not None and 0 <= slot < capacity and slot not in taken:
            slots[key] = slot
            taken.add(slot)
    free = (slot for slot in range(capacity) if slot not in taken)
    for key in windows:
        if key in slots:
            continue
        slot = next(free, None)
        if slot is None:
            break
        slots[key] = slot
    return slots


def _best_grid(
    inner: Rect, count: int, *, max_columns: int, max_rows: int, gap: int
) -> tuple[int, int]:
    """Сетка, чья ячейка по пропорциям ближе всего к области; при равенстве — меньше пустых."""
    target = math.log(inner.width / inner.height)
    best: tuple[float, int, int, int] | None = None
    for columns in range(1, min(count, max_columns) + 1):
        rows = math.ceil(count / columns)
        if rows > max_rows:
            continue
        width = (inner.width - (columns - 1) * gap) / columns
        height = (inner.height - (rows - 1) * gap) / rows
        score = (abs(math.log(width / height) - target), columns * rows - count, columns)
        if best is None or score < best[:3]:
            best = (*score, rows)
    if best is None:  # pragma: no cover — count не больше ёмкости области
        msg = f"{count} ячеек не помещаются в {inner}"
        raise ValueError(msg)
    return best[2], best[3]


def _fit(length: int, cell: int, gap: int) -> int:
    """Сколько отрезков `cell` с зазором `gap` помещается в `length`."""
    return max(0, (length + gap) // (cell + gap))


def _inset(area: Rect, margin: int) -> Rect | None:
    width, height = area.width - 2 * margin, area.height - 2 * margin
    if width <= 0 or height <= 0:
        return None
    return Rect(x=area.x + margin, y=area.y + margin, width=width, height=height)


__all__ = ["LayoutEngine", "LayoutKind", "LayoutPolicy", "Plan", "Reflow"]
