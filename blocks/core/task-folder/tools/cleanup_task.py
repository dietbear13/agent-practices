#!/usr/bin/env python3
"""Чистильщик папки задачи: удаляет всё, чего нет в `manifest.json`.

Только стандартная библиотека — живёт в проекте пользователя отдельно от
конструктора.

    python tools/cleanup_task.py tasks/2026-09-05-schema-audit/
    python tools/cleanup_task.py tasks/2026-09-05-schema-audit/ --apply

Без `--apply` — только показывает, что удалил бы. Удаление файлов не должно быть
побочным эффектом любопытства: посмотреть, что накопилось, человек захочет чаще, чем
чистить.

Почему манифест, а не список расширений: агент в процессе работы создаёт черновики,
промежуточные выгрузки и «отчёт_финальный_2.csv», и через месяц никто не помнит, что из
этого результат. Манифест ведёт код, который писал результат, — он знает точно.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

# До любого print — иначе на Windows кириллица в выводе превращается в мусор или роняет
# UnicodeEncodeError при перенаправлении (та же причина, что в _runtime.py и скриптах
# сборщика).
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

# Служебные файлы прогона: их нет в manifest.json (манифест — список результатов, а не
# самого себя), но удалять их нельзя. `*.quality.json` — метаданные качества: период,
# число строк, sampled, complete. Без них выгрузка перестаёт быть проверяемой, а именно
# проверяемость отличает результат от «какого-то файла». Чистильщик, сносящий метаданные,
# аккуратно уничтожает то единственное, ради чего каркас их пишет.
PROTECTED_PATTERNS = (
    "manifest.json",
    "call_log.json",
    ".checkpoint.json",
    "*.quality.json",
)


def is_protected(relative_path: str) -> bool:
    name = Path(relative_path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in PROTECTED_PATTERNS)


def load_manifest_paths(task_dir: Path) -> set[str]:
    """Читает список файлов, которые считаются результатом.

    Поддерживаются две формы, потому что манифест ведут двое:

      {"keep": ["report.md", "fixes.csv"]}   — разовая задача, список ведёт человек
      [{"path": "report.csv", ...}, ...]     — регулярная, список ведёт runtime.py

    Обе означают одно: всё, чего здесь нет, — черновик и мусор.
    """
    manifest_path = task_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"{manifest_path} не найден. Чистильщик работает только по манифесту: "
            "без него он не может отличить результат от мусора и поэтому не удаляет ничего."
        )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        entries = data.get("keep")
        if entries is None:
            raise ValueError(
                f"{manifest_path}: в объекте нет ключа keep — перечислите в нём файлы-результаты"
            )
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"{manifest_path}: ожидается список записей или объект с ключом keep")

    paths: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path"):
            paths.add(str(entry["path"]).replace("\\", "/"))
        elif isinstance(entry, str):
            paths.add(entry.replace("\\", "/"))
    return paths


def find_strays(task_dir: Path, manifest_paths: set[str]) -> list[Path]:
    strays: list[Path] = []
    for path in sorted(task_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(task_dir)).replace("\\", "/")
        if relative in manifest_paths or is_protected(relative):
            continue
        strays.append(path)
    return strays


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path, help="папка задачи с manifest.json внутри")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="действительно удалить (без флага — только показать)",
    )
    args = parser.parse_args(argv)

    task_dir = args.task_dir.resolve()
    if not task_dir.is_dir():
        print(f"Ошибка: папка задачи не найдена: {task_dir}", file=sys.stderr)
        return 1

    try:
        manifest_paths = load_manifest_paths(task_dir)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    strays = find_strays(task_dir, manifest_paths)

    missing = sorted(p for p in manifest_paths if not (task_dir / p).exists())
    if missing:
        print(
            "Внимание: файлы есть в манифесте, но отсутствуют на диске: "
            + ", ".join(missing)
            + ". Их кто-то удалил мимо чистильщика — результат прогона неполон."
        )

    if not strays:
        print(f"Чисто: посторонних файлов нет ({len(manifest_paths)} в манифесте).")
        return 0

    print(f"Посторонних файлов: {len(strays)} (нет в manifest.json)")
    for path in strays:
        print(f"  {path.relative_to(task_dir)}")

    if not args.apply:
        print("\n(показ — ничего не удалено; повторите с --apply)")
        return 0

    for path in strays:
        path.unlink()
    print(f"\nУдалено файлов: {len(strays)}. Результаты из манифеста и метаданные качества не тронуты.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
