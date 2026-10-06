"""Имя файла из произвольного ключа: читаемая часть плюс хеш точного ключа.

Ключи identity — `gmx:Ada@example.com`, кириллица, любой регистр. Файловые системы Windows и macOS
не различают регистр, а имя файла ограничено по длине, поэтому имя = обрезанная читаемая часть +
хеш ключа как он есть: разные ключи не совпадают ни по регистру, ни по обрезке.
"""

from __future__ import annotations

import hashlib
import re

_READABLE_MAX = 64


def safe_file_name(key: str) -> str:
    """Имя файла из ключа (без расширения): читаемая часть и хеш — разные ключи не совпадут."""
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._")[:_READABLE_MAX]
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"{readable}-{digest}" if readable else digest


__all__ = ["safe_file_name"]
