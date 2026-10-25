#!/usr/bin/env python3
"""Заводит папку под разовую задачу: `tasks/ГГГГ-ММ-ДД-имя/`.

    python tools/new_task.py schema-audit
    python tools/new_task.py "аудит микроразметки" --date 2026-09-05

Зачем отдельный скрипт на такую мелочь: разовые задачи засоряют проект быстрее
всего остального, и лечится это не аккуратностью, а тем, что завести папку
проще, чем не завести. Заодно сразу появляется `manifest.json` — без него
чистильщик потом не сможет отличить результат от черновика и не тронет ничего.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

REPORT_TEMPLATE = """# {title}

Дата: {day}

## Что делаем

<!-- Задача словами: что нужно получить и зачем. -->

## Вводные

<!-- Ссылки, выгрузки, доступы. Файлы кладите в эту же папку. -->

## Результат

<!-- Итог задачи. Он остаётся здесь же, а не переезжает в корень проекта. -->
"""


def slugify(text: str) -> str:
    """Латинский слаг из русского названия: имя папки должно быть переносимым.

    Кириллица в путях работает, но ломается ровно там, где чинить дороже всего:
    в архивах, в чужой консоли, в CI. Название целиком остаётся в report.md.
    """
    lowered = text.strip().lower()
    out = "".join(TRANSLIT.get(ch, ch) for ch in lowered)
    out = unicodedata.normalize("NFKD", out).encode("ascii", "ignore").decode("ascii")
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out or "task"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Новая папка разовой задачи")
    parser.add_argument("name", help="название задачи, можно по-русски")
    parser.add_argument("--date", help="дата ГГГГ-ММ-ДД, по умолчанию сегодня")
    parser.add_argument("--root", default="tasks", help="корень папок задач")
    args = parser.parse_args(argv)

    day = args.date or date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        print(f"Ошибка: дата должна быть в виде ГГГГ-ММ-ДД, получено: {day}", file=sys.stderr)
        return 1

    folder = Path(args.root) / f"{day}-{slugify(args.name)}"
    if folder.exists():
        print(f"Папка уже есть: {folder}")
        return 0

    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        json.dumps({"keep": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (folder / "report.md").write_text(
        REPORT_TEMPLATE.format(title=args.name, day=day), encoding="utf-8"
    )

    print(f"Готово: {folder}")
    print("Результаты и черновики держите здесь же — в корень проекта они не переезжают.")
    print("Когда закончите: python tools/cleanup_task.py " + str(folder))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
