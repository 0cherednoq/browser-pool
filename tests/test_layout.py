"""Раскладка окон: без перекрытий, внутри области, с зазором, детерминированно, стабильно."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from browser_pool.geometry import Rect
from browser_pool.windows import LayoutEngine, LayoutPolicy

FULL_HD = Rect(x=0, y=0, width=1920, height=1040)
SECOND = Rect(x=1920, y=0, width=1280, height=1000)


def keys(count: int, prefix: str = "w") -> list[str]:
    return [f"{prefix}{index}" for index in range(count)]


def separated(first: Rect, second: Rect, gap: int) -> bool:
    """Прямоугольники не пересекаются и между ними не меньше `gap`."""
    return (
        first.x + first.width + gap <= second.x
        or second.x + second.width + gap <= first.x
        or first.y + first.height + gap <= second.y
        or second.y + second.height + gap <= first.y
    )


def inside(rect: Rect, area: Rect, margin: int) -> bool:
    return (
        rect.x >= area.x + margin
        and rect.y >= area.y + margin
        and rect.x + rect.width <= area.x + area.width - margin
        and rect.y + rect.height <= area.y + area.height - margin
    )


# --- примеры ---------------------------------------------------------------------------


def test_six_windows_make_a_grid_not_a_row() -> None:
    plan = LayoutEngine(LayoutPolicy(reflow="fill", gap=0)).plan([FULL_HD], keys(6))

    rows = {rect.y for rect in plan.rects.values()}
    columns = {rect.x for rect in plan.rects.values()}
    assert (len(columns), len(rows)) == (3, 2)
    assert plan.overflow == ()


def test_capacity_follows_min_size_and_max_windows() -> None:
    engine = LayoutEngine(LayoutPolicy(min_size=(480, 360), gap=0))

    assert engine.capacity([FULL_HD]) == 4 * 2
    assert engine.capacity([FULL_HD, SECOND]) == 4 * 2 + 2 * 2
    assert LayoutEngine(LayoutPolicy(max_windows=5)).capacity([FULL_HD]) == 5


def test_windows_beyond_capacity_overflow_in_order() -> None:
    plan = LayoutEngine(LayoutPolicy(max_windows=4)).plan([FULL_HD], keys(6))

    assert set(plan.rects) == {"w0", "w1", "w2", "w3"}
    assert plan.overflow == ("w4", "w5")


def test_second_screen_takes_the_rest() -> None:
    engine = LayoutEngine(LayoutPolicy(min_size=(900, 500), gap=0))

    plan = engine.plan([FULL_HD, SECOND], keys(5))

    on_second = [key for key, rect in plan.rects.items() if rect.x >= SECOND.x]
    assert len(on_second) == 1  # первый монитор вмещает 2×2, пятое окно — на втором
    assert plan.overflow == ()


def test_stable_reflow_keeps_living_windows_in_place() -> None:
    engine = LayoutEngine(LayoutPolicy(max_windows=6))
    first = engine.plan([FULL_HD], keys(4))

    second = engine.plan([FULL_HD], ["w0", "w2", "w3", "new"], previous=first.slots)

    for key in ("w0", "w2", "w3"):
        assert second.rects[key] == first.rects[key]
    assert second.rects["new"] == first.rects["w1"]  # освободившаяся ячейка переиспользуется


def test_fill_reflow_uses_the_whole_screen() -> None:
    engine = LayoutEngine(LayoutPolicy(reflow="fill", gap=0))

    (only,) = engine.plan([FULL_HD], ["w0"]).rects.values()

    assert only == FULL_HD


def test_fixed_size_windows_are_packed_from_the_corner() -> None:
    engine = LayoutEngine(LayoutPolicy(size=(800, 600), gap=10, margin=20))

    plan = engine.plan([FULL_HD], keys(3))

    assert [rect.width for rect in plan.rects.values()] == [800, 800]
    assert plan.rects["w0"] == Rect(x=20, y=20, width=800, height=600)
    assert plan.rects["w1"].x == 20 + 800 + 10
    assert plan.overflow == ("w2",)  # по высоте помещается один ряд, по ширине — два


def test_cascade_steps_down_inside_the_area() -> None:
    plan = LayoutEngine(LayoutPolicy(layout="cascade")).plan([FULL_HD], keys(3))

    first, second, _ = plan.rects.values()
    assert (second.x - first.x, second.y - first.y) == (32, 32)
    assert all(inside(rect, FULL_HD, 0) for rect in plan.rects.values())


def test_too_small_area_puts_everything_in_overflow() -> None:
    tiny = Rect(x=0, y=0, width=300, height=200)

    plan = LayoutEngine().plan([tiny], keys(2))

    assert plan.rects == {}
    assert plan.overflow == ("w0", "w1")


@pytest.mark.parametrize(
    ("policy", "fragment"),
    [
        ({"layout": "spiral"}, "layout"),
        ({"reflow": "sometimes"}, "reflow"),
        ({"size": (100, 100)}, "min_size"),
        ({"gap": -1}, "gap"),
        ({"max_windows": 0}, "max_windows"),
    ],
)
def test_bad_policy_is_explained(policy: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        LayoutPolicy(**policy)  # pyright: ignore[reportArgumentType] — нарочно неверные значения


def test_repeated_windows_are_refused() -> None:
    with pytest.raises(ValueError, match="повторяются"):
        LayoutEngine().plan([FULL_HD], ["w0", "w0"])


# --- свойства --------------------------------------------------------------------------

sizes = st.tuples(st.integers(200, 700), st.integers(150, 500))
policies = st.builds(
    LayoutPolicy,
    layout=st.sampled_from(["grid", "columns", "rows"]),
    reflow=st.sampled_from(["stable", "fill"]),
    min_size=sizes,
    gap=st.integers(0, 24),
    margin=st.integers(0, 40),
    max_windows=st.none() | st.integers(1, 12),
)
areas = st.lists(
    st.builds(
        Rect,
        x=st.integers(-2000, 4000),
        y=st.integers(-500, 1500),
        width=st.integers(300, 3840),
        height=st.integers(200, 2160),
    ),
    min_size=1,
    max_size=3,
)


def side_by_side(screens: list[Rect]) -> list[Rect]:
    """Мониторы рядом друг с другом, как у настоящего рабочего стола."""
    placed: list[Rect] = []
    x = 0
    for screen in screens:
        placed.append(Rect(x=x, y=screen.y, width=screen.width, height=screen.height))
        x += screen.width
    return placed


@settings(max_examples=300, deadline=None)
@given(policy=policies, screens=areas, count=st.integers(0, 30))
def test_cells_never_overlap_and_stay_inside(
    policy: LayoutPolicy, screens: list[Rect], count: int
) -> None:
    screens = side_by_side(screens)
    engine = LayoutEngine(policy)
    windows = keys(count)

    plan = engine.plan(screens, windows)

    assert len(plan.rects) == min(count, engine.capacity(screens))
    assert set(plan.rects) | set(plan.overflow) == set(windows)
    for rect in plan.rects.values():
        assert any(inside(rect, screen, policy.margin) for screen in screens)
        assert rect.width >= policy.min_size[0]
        assert rect.height >= policy.min_size[1]
    for first, second in itertools.combinations(plan.rects.values(), 2):
        # Зазор — между окнами одного монитора; на соседних они могут касаться по общей границе.
        same_screen = any(inside(first, s, 0) and inside(second, s, 0) for s in screens)
        assert separated(first, second, policy.gap if same_screen else 0)
    assert engine.plan(screens, windows) == plan  # детерминизм


@settings(max_examples=200, deadline=None)
@given(
    policy=policies.filter(lambda policy: policy.reflow == "stable"),
    screens=areas,
    initial=st.integers(0, 12),
    removed=st.sets(st.integers(0, 11)),
    added=st.integers(0, 6),
)
def test_stable_reflow_never_moves_a_living_window(
    *, policy: LayoutPolicy, screens: list[Rect], initial: int, removed: set[int], added: int
) -> None:
    screens = side_by_side(screens)
    engine = LayoutEngine(policy)
    before = engine.plan(screens, keys(initial))
    survivors = [key for index, key in enumerate(keys(initial)) if index not in removed]

    after = engine.plan(screens, survivors + keys(added, "new"), previous=before.slots)

    for key in survivors:
        if key in before.rects:
            assert after.rects[key] == before.rects[key]


def test_fixed_columns_make_rows_of_that_width() -> None:
    engine = LayoutEngine(LayoutPolicy(columns=3, gap=0))
    area = Rect(x=0, y=0, width=1920, height=1200)

    plan = engine.plan([area], [f"w{index}" for index in range(9)])

    rows = sorted({rect.y for rect in plan.rects.values()})
    assert len(rows) == 3
    assert [plan.rects[f"w{index}"].y for index in range(3)] == [rows[0]] * 3
    assert engine.capacity([area]) == 9
