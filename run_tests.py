"""Прогон тестов с правильной кодировкой вывода.

`python -m unittest` пишет имена и докстроки тестов в stderr раньше, чем наши
скрипты успевают настроить кодировку, и на Windows русские названия
превращаются в мусор. Этот раннер настраивает потоки первым делом.

    python run_tests.py
    python run_tests.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bootstrap"))

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main() -> int:
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stderr)
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
