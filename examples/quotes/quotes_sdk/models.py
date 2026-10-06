"""Что SDK отдаёт наружу."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Quote:
    """Цитата и её автор."""

    text: str
    author: str


@dataclass(frozen=True, slots=True)
class QuotesPage:
    """Одна прочитанная страница цитат."""

    number: int
    quotes: tuple[Quote, ...]
