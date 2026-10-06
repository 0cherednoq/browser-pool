"""Примеры страницы «Начало работы»: три шага от одной вкладки до входа на сайт.

    uv run python -m examples.start.minimal
    uv run python -m examples.start.readers
    uv run python -m examples.start.login

Нужны экстра `playwright`, `playwright install chromium` и доступ к quotes.toscrape.com: это
учебный сайт для автоматизации, его вход принимает любой логин и пароль. Страница документации
подключает эти файлы целиком, а их вывод сверяет `tests/test_examples.py`.
"""
