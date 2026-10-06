"""Страницы сайта оформлены по единым правилам.

Сборка и линтер прозы не видят, удобно ли страницу читать. Здесь проверяется то, что можно
посчитать: заголовок помещается в строку бокового меню, под ним стоит лид, цветных блоков немного,
страница не обрывается, в комментариях примеров нет длинного тире.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SECTIONS = ("tutorial", "guide", "extending", "explanation")
# Оглавление каталога для GitHub: в сайт не входит (`exclude_patterns` в `docs/conf.py`).
OUTSIDE_SITE = {DOCS / "guide" / "README.md"}
PAGES = sorted(
    path for name in SECTIONS for path in (DOCS / name).glob("*.md") if path not in OUTSIDE_SITE
)

TITLE_LIMIT = 25
LEAD_LIMIT = 100
ADMONITIONS_LIMIT = 3
CARDS_AT_LEAST = 2
LONG_DASH = "—"
ADMONITION = re.compile(r"^:{3,}\{(warning|important|tip|note)\}", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

page = pytest.mark.parametrize("path", PAGES, ids=lambda path: path.relative_to(DOCS).as_posix())


def test_there_are_pages_to_check() -> None:
    assert {path.parent.name for path in PAGES} == set(SECTIONS)


@page
def test_title_fits_one_line_of_the_sidebar(path: Path) -> None:
    title = path.read_text(encoding="utf-8").split("\n", 1)[0]

    assert title.startswith("# ")
    assert len(title) - 2 <= TITLE_LIMIT, title


@page
def test_page_opens_with_a_one_line_lead(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").split("\n")

    assert lines[1] == "", "после заголовка пустая строка"
    assert lines[2], "под заголовком лид"
    assert lines[3] == "", "лид занимает одну строку"
    assert not lines[2].startswith(("#", "`", ":", "|", "-")), "лид - обычный абзац"
    assert len(lines[2]) <= LEAD_LIMIT, lines[2]


@page
def test_page_ends_with_what_is_next(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    tail = text.rsplit("\n## ", 1)[-1]

    assert tail.startswith("Что дальше\n"), "последний раздел страницы - «Что дальше»"
    assert tail.count("{grid-item-card}") >= CARDS_AT_LEAST, "в нём хотя бы две карточки"


@page
def test_there_are_few_admonitions(path: Path) -> None:
    found = ADMONITION.findall(path.read_text(encoding="utf-8"))

    assert len(found) <= ADMONITIONS_LIMIT, found


@page
def test_code_on_the_page_has_no_long_dash(path: Path) -> None:
    blocks = FENCE.findall(path.read_text(encoding="utf-8"))
    lines = [line for block in blocks for line in block.split("\n") if LONG_DASH in line]

    assert not lines, lines
