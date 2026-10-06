"""`python -m examples.accounts.app` — сценарий из `scenario.py`: печатает итог, код выхода 0 — приёмка пройдена."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from examples.accounts.app.scenario import run


def main() -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(
        description="browser-pool + mail_sdk: аккаунты, прокси, падение"
    )
    parser.add_argument("--driver", choices=["playwright", "pydoll"], default="playwright")
    parser.add_argument("--accounts", type=int, default=6)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="логи пула уровня INFO")
    parser.add_argument("--dump-after", type=float, help=argparse.SUPPRESS)
    options = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if options.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    report = asyncio.run(
        run(
            driver=options.driver,
            accounts=options.accounts,
            rounds=options.rounds,
            headless=not options.headed,
            dump_after=options.dump_after,
        )
    )
    if options.json:
        print(json.dumps({**asdict(report), "passed": report.passed}))
    else:
        for name, value in asdict(report).items():
            print(f"{name:>18}: {value}")
        print(f"{'passed':>18}: {report.passed}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
