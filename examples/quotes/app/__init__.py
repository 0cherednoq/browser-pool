"""Оркестрация — слой 2: берёт `quotes_sdk` и `browser_pool` и собирает из них приложение.

Здесь и только здесь встречаются оба мира: flow (`flow.py`) открывает сессию вызовами SDK,
`errors.py` переводит ошибки SDK в виды сбоя пула, `pool.py` собирает пул, `__main__.py` — работа.

    uv run --extra playwright python -m examples.quotes.app --mode headless
"""
