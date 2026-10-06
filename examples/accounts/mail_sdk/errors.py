"""Ошибки почты — доменные: что случилось с точки зрения сайта."""

from __future__ import annotations


class MailError(Exception):
    """Любая ошибка почты."""


class LoginFailedError(MailError):
    """Пароль не подошёл: сайт остался на форме входа."""


class NotLoggedInError(MailError):
    """Операция требует входа, а сайт отправил на форму входа."""


class WrongMailboxError(MailError):
    """Открылся чужой ящик: в контексте сессия другого аккаунта."""
