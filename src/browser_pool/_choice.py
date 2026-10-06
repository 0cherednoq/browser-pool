"""Проверка в рантайме, что поле — одно из значений `Literal`-псевдонима.

Типизатор ловит неверную строку только у того, кто им пользуется; публичные значения проверяют
себя сами, как остальные (`DriverCapabilities`, `Proxy`, конфиг). Модуль без зависимостей:
листья (`state`, `proxies`) тоже его читают.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

if TYPE_CHECKING:
    from typing import TypeAliasType


def require_choice(
    label: str, value: object, alias: TypeAliasType, *, error: type[ValueError] = ValueError
) -> None:
    """`value` — одно из значений `type alias = Literal[...]`, иначе `error` с именем поля `label`."""
    allowed = get_args(alias.__value__)
    if value not in allowed:
        options = ", ".join(repr(option) for option in allowed)
        msg = f"{label}: допустимо {options}, получено {value!r}"
        raise error(msg)


def check_choice(
    owner: object, name: str, alias: TypeAliasType, *, error: type[ValueError] = ValueError
) -> None:
    """Поле `name` объекта `owner` — одно из значений `type alias = Literal[...]`, иначе `error`."""
    require_choice(f"{type(owner).__name__}.{name}", getattr(owner, name), alias, error=error)
