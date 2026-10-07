"""Настройка сайта документации (Sphinx): `uv run poe site`.

Источник — каталог `docs/`. Что в сайт не входит, перечислено в `exclude_patterns`.
"""

from __future__ import annotations

import os
import re
from importlib.metadata import version as _installed_version
from pathlib import Path
from typing import TYPE_CHECKING

from docutils import nodes
from sphinx import addnodes
from sphinxcontrib.mermaid import mermaid

if TYPE_CHECKING:
    from sphinx.application import Sphinx

project = "browser-pool"
author = "browser-pool contributors"
project_copyright = "2026, browser-pool contributors"
release = _installed_version("browser-pool")
version = release

language = "ru"
root_doc = "index"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
    "sphinx_design",
    "sphinx_copybutton",
    "sphinxcontrib.mermaid",
    "sphinx_llm_friendly",
]

# Рабочие документы называются заглавными буквами и лежат в корне `docs/`; страницы сайта — строчными.
# `guide/README.md` — оглавление каталога для GitHub; на сайте его заменяет боковое меню.
exclude_patterns = [
    "_build",
    "_locale",
    "raw",
    "[A-Z]*.md",
    "guide/README.md",
]

# У темы нет русского перевода: свои подписи интерфейса лежат в `_locale/ru/LC_MESSAGES/sphinx.po`.
locale_dirs = ["_locale"]

# Докстринги написаны с одинарными обратными кавычками, как Markdown: в RST это роль по умолчанию.
default_role = "code"

add_module_names = False
autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_class_signature = "separated"

# Битая ссылка на класс или страницу — ошибка сборки, а не тихий пропуск.
nitpicky = True
# Чего в справочнике нет намеренно: параметры типов, классы чужих SDK, приватные имена. Имена,
# импортированные под TYPE_CHECKING (`datetime`, `Path`), autodoc видит без модуля — они остаются текстом.
nitpick_ignore_regex = [
    ("py:class", r"[A-Z]"),
    ("py:class", r"A\.(args|kwargs)"),
    ("py:obj", r"typing\.Args"),
    ("py:class", r"(datetime|Path|Executor)"),
    ("py:class", r"(Browser|BrowserContext|Page|Tab|CollectorRegistry)"),
    ("py:class", r"(ContractSite|LeaseControl)"),
    ("py:class", r"browser_pool\.(\w+\.)*_\w+"),
    # `asyncio.SelectorEventLoop` — на каждой ОС свой приватный класс.
    ("py:class", r"asyncio\.windows_events\._WindowsSelectorEventLoop"),
    ("py:class", r"asyncio\.unix_events\._UnixSelectorEventLoop"),
]

myst_enable_extensions = [
    "attrs_block",
    "attrs_inline",
    "colon_fence",
    "deflist",
]
myst_heading_anchors = 3

intersphinx_mapping = {
    "python": ("https://docs.python.org/3/", None),
}

html_theme = "shibuya"
html_title = "browser-pool"
html_show_sourcelink = False
html_static_path = ["_static"]
html_css_files = ["site.css"]
html_theme_options = {
    "accent_color": "teal",
    "globaltoc_expand_depth": 1,
    "github_url": "https://github.com/0cherednoq/browser-pool",
    # Кнопку «скопировать как Markdown» ставит `sphinx_llm_friendly`; вторая от темы не нужна.
    "show_ai_links": False,
}
# Карточка репозитория и ссылка «Править страницу» в правой колонке.
html_context = {
    "source_type": "github",
    "source_user": "0cherednoq",
    "source_repo": "browser-pool",
    "source_version": "main",
    "source_docs_path": "/docs/",
}

# `sphinx_llm_friendly` кладёт рядом с каждой страницей её Markdown, а в корень сайта — `llms.txt`
# и `llms-full.txt`. Ссылки в `llms.txt` абсолютные: Read the Docs называет адрес собираемой версии.
html_baseurl = os.environ.get(
    "READTHEDOCS_CANONICAL_URL", "https://browser-pool.readthedocs.io/ru/latest/"
)
llm_friendly_llms_txt_summary = (
    f"Документация browser-pool версии {release}: пул браузеров для асинхронного Python. "
    "Вся документация одним файлом лежит рядом, в `llms-full.txt`."
)

# ---------- Ссылки на файлы репозитория ----------
# Страницы читаются и на GitHub, поэтому ссылаются на код относительными путями
# (`../../examples/quickstart.py`). На сайте этих файлов нет: такие ссылки ведут в репозиторий.

REPOSITORY = "https://github.com/0cherednoq/browser-pool"
# Read the Docs собирает тег или ветку и называет их в окружении; локально — основная ветка.
REVISION = os.environ.get("READTHEDOCS_GIT_IDENTIFIER", "main")

_DOCS = Path(__file__).resolve().parent
_ROOT = _DOCS.parent
_RELATIVE_LINK = re.compile(r"\]\((?P<path>\.\.?/[^)#\s]+)(?P<anchor>#[^)\s]*)?\)")


def _to_repository(app: Sphinx, docname: str, source: list[str]) -> None:
    """Относительную ссылку на файл вне сайта заменить адресом этого файла в репозитории."""
    page_dir = (_DOCS / docname).parent

    def replace(match: re.Match[str]) -> str:
        target = (page_dir / match["path"]).resolve()
        if not target.exists() or not target.is_relative_to(_ROOT):
            return match[0]  # несуществующее Sphinx назовёт сам
        if target.is_relative_to(_DOCS):
            page = target.relative_to(_DOCS).with_suffix("").as_posix()
            if page in app.env.found_docs:
                return match[0]  # страница сайта
        kind = "tree" if target.is_dir() else "blob"
        path = target.relative_to(_ROOT).as_posix()
        return f"]({REPOSITORY}/{kind}/{REVISION}/{path}{match['anchor'] or ''})"

    source[0] = _RELATIVE_LINK.sub(replace, source[0])


# ---------- Примеры в докстрингах ----------
# Докстринги кода написаны как Markdown: пример идёт блоком с отступом после пустой строки. В RST
# такой блок без `::` перед ним — ошибка разметки, поэтому перед сборкой `::` дописывается.


def _literal_blocks(_app: Sphinx, _what: str, _name: str, _obj: object, _options: object, lines: list[str]) -> None:
    """Блок с отступом после обычного абзаца сделать литеральным."""
    result: list[str] = []
    previous = ""  # последняя непустая строка
    for index, line in enumerate(lines):
        starts_block = (
            line.startswith("    ")
            and index > 0
            and not lines[index - 1].strip()
            and previous
            and not previous.startswith(" ")
            and not previous.rstrip().endswith("::")
        )
        if starts_block:
            result.extend(["::", ""])
        result.append(line)
        if line.strip():
            previous = line
    lines[:] = result


# ---------- Ссылки из текста в справочник API ----------
# Имя в обратных кавычках (`BrowserPool`, `Limits.max_waiting`, `pool.snapshot()`) становится ссылкой
# на справочник, если такое имя там одно. Страницы от этого не меняются и читаются на GitHub как есть.

_API_NAME = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)(\(\))?")
# Как в примерах называют экземпляры: `pool.page()` — это `BrowserPool.page`.
_INSTANCES = {"pool": "BrowserPool", "lease": "PageLease", "snapshot": "PoolSnapshot"}
_api_index: dict[str, tuple[str, str] | None] = {}


def _api_targets(app: Sphinx) -> dict[str, tuple[str, str] | None]:
    """Короткое имя → (страница, якорь); `None` — имя неоднозначно, ссылки не будет."""
    if _api_index:
        return _api_index
    for fullname, entry in app.env.domains.python_domain.objects.items():
        if entry.aliased or entry.objtype == "module":
            continue
        parts = fullname.split(".")
        target = (entry.docname, entry.node_id)
        member = entry.objtype in {"method", "attribute", "property"}
        keys = {fullname, ".".join(parts[-2:]) if member else parts[-1]}
        for key in keys:
            _api_index[key] = None if key in _api_index and _api_index[key] != target else target
    return _api_index


def _link_api_names(app: Sphinx, doctree: nodes.document, docname: str) -> None:
    """Имена из справочника, набранные как код, превратить в ссылки на него."""
    if app.builder.format != "html":
        return
    targets = _api_targets(app)
    skipped = (nodes.reference, nodes.title, addnodes.desc_signature, nodes.literal_block)
    for literal in list(doctree.findall(nodes.literal)):
        match = _API_NAME.fullmatch(literal.astext())
        if match is None:
            continue
        parent = literal.parent
        inside = False
        while parent is not None and not inside:
            inside = isinstance(parent, skipped)
            parent = parent.parent
        if inside:
            continue
        name = match["name"]
        head, _, rest = name.partition(".")
        if head in _INSTANCES and rest:
            name = f"{_INSTANCES[head]}.{rest}"
        target = targets.get(name)
        if target is None:
            continue
        page, anchor = target
        uri = f"{app.builder.get_relative_uri(docname, page)}#{anchor}"
        link = nodes.reference("", "", internal=True, refuri=uri, classes=["api-link"])
        literal.replace_self(link)
        link += literal


# ---------- Диаграммы в Markdown-версии страниц ----------
# `sphinx_llm_friendly` не знает узел Mermaid: в `llms.txt` диаграмма уходит своим исходным текстом.


def _mermaid_as_markdown(translator: nodes.NodeVisitor, node: nodes.Element) -> None:
    block = "\n".join(["```mermaid", node["code"], "```"])
    translator.add(block, prefix_eol=1, suffix_eol=2)  # type: ignore[attr-defined]
    raise nodes.SkipNode


def setup(app: Sphinx) -> None:
    """Подключить обработчики сборки."""
    app.connect("source-read", _to_repository)
    app.connect("autodoc-process-docstring", _literal_blocks)
    app.connect("doctree-resolved", _link_api_names)
    app.add_node(mermaid, override=True, llm_markdown=(_mermaid_as_markdown, None))
