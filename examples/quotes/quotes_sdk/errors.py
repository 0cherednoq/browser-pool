"""Ошибки сайта — доменные: что случилось с точки зрения сайта, а не браузера и не пула."""

from __future__ import annotations


class QuotesError(Exception):
    """Любая ошибка сайта quotes.toscrape.com."""


class LoginFailedError(QuotesError):
    """Сайт не принял вход: после отправки формы ссылки «Logout» нет."""


class SessionExpiredError(QuotesError):
    """Операция требует входа, а сайт считает вкладку гостевой."""


class SiteChangedError(QuotesError):
    """Разметка не та, что ждёт SDK: на странице нет цитат, где они должны быть."""
