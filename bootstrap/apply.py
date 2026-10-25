"""Разворачивание блоков в проект пользователя.

По умолчанию — сухой прогон: показывает, что произойдёт, и ничего не пишет.
Запись только по явному --apply.

Три класса файлов, и класс задан в каталоге, а не соглашением в документации:

  generated — наш файл, обновляется молча, НО только если человек его не трогал.
              Тронул (хеш разошёлся с записанным при установке) — новая версия
              ложится рядом с суффиксом .new, оригинал остаётся.
  owned     — файл человека с нашей заготовки: CLAUDE.md, PRIORITIES.md, тела
              скиллов. После установки не перезаписывается никогда. Обновление
              каталога тоже кладёт .new рядом.
  (user)    — tasks/, outputs/, temp/, .env: этих файлов здесь нет вообще,
              скрипт про них не знает и знать не должен.

Флага «перезаписать всё» нет намеренно. Именно он превращает регенерацию в
потерю месяца правок, после которой к конструктору больше не возвращаются.

    python bootstrap/apply.py --target ../my-project --blocks core/project-files
    python bootstrap/apply.py --target ../my-project --preset minimal --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EXIT_CONFIRM,
    EXIT_USER,
    Result,
    die,
    is_inside,
    read_json,
    read_text,
    repo_root,
    setup_io,
    sha256_text,
    substitute,
    write_json_atomic,
    write_text_atomic,
)

STATE_DIR = ".agent-kit"
STATE_FILE = "state.json"
STATE_SCHEMA = 1

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_SKIP = "skip"
ACTION_SIDECAR = "sidecar"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(target: Path) -> Path:
    return target / STATE_DIR / STATE_FILE


def load_state(target: Path) -> Dict[str, Any]:
    path = state_path(target)
    if path.is_file():
        return read_json(path)
    return {"schema_version": STATE_SCHEMA, "installed": {}, "blocks": {}}


def resolve_blocks(
    catalog: Dict[str, Any], ids: List[str], preset: Optional[str]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Собирает блоки по именам и пресету, дотягивая зависимости."""
    by_id = {b["id"]: b for b in catalog["blocks"]}
    wanted: List[str] = list(ids)
    if preset:
        wanted += catalog["presets"].get(preset, [])

    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        return [], unknown

    ordered: List[str] = []

    def add(block_id: str, seen: Tuple[str, ...] = ()) -> None:
        if block_id in ordered:
            return
        if block_id in seen:
            raise ValueError(f"циклическая зависимость: {' -> '.join(seen + (block_id,))}")
        for dep in by_id[block_id].get("requires", []):
            add(dep, seen + (block_id,))
        ordered.append(block_id)

    for block_id in wanted:
        add(block_id)
    return [by_id[i] for i in ordered], []


def plan_file(
    target: Path,
    state: Dict[str, Any],
    block: Dict[str, Any],
    entry: Dict[str, Any],
    rendered: str,
) -> Tuple[str, str]:
    """Решает, что сделать с одним файлом. Возвращает (действие, объяснение)."""
    dst = target / entry["dst"]
    record = state.get("installed", {}).get(entry["dst"])
    new_hash = sha256_text(rendered)

    if not dst.exists():
        return ACTION_CREATE, "файла нет — создаём"

    current_hash = sha256_text(read_text(dst))

    if current_hash == new_hash:
        return ACTION_SKIP, "уже на месте, содержимое совпадает"

    if record is None:
        return (
            ACTION_SIDECAR,
            "файл существует, но ставили его не мы — своё положим рядом как .new",
        )

    if record.get("sha256") != current_hash:
        return (
            ACTION_SIDECAR,
            "файл правлен после установки — правка остаётся, новая версия рядом как .new",
        )

    if entry["class"] == "owned":
        return (
            ACTION_SIDECAR,
            "файл ваш по смыслу (owned) — не перезаписываем, новая версия рядом как .new",
        )

    return ACTION_UPDATE, "наш файл, не тронут — обновляем"


def run(
    root: Path,
    target: Path,
    block_ids: List[str],
    preset: Optional[str],
    values: Dict[str, str],
    do_apply: bool,
) -> int:
    action = "apply" if do_apply else "apply.dry-run"
    result = Result(action=action)

    catalog_path = root / "catalog.json"
    if not catalog_path.is_file():
        return die(action, "catalog.json отсутствует", "python bootstrap/catalog.py --build")
    catalog = read_json(catalog_path)

    if is_inside(target, root):
        return die(
            action,
            "целевая папка находится внутри репозитория конструктора",
            "укажите путь вне этого репозитория — рабочее пространство всегда отдельно",
        )

    try:
        blocks, unknown = resolve_blocks(catalog, block_ids, preset)
    except ValueError as exc:
        return die(action, str(exc))
    if unknown:
        return die(
            action,
            f"неизвестные блоки: {', '.join(unknown)}",
            "список: python bootstrap/catalog.py --list",
        )
    if not blocks:
        return die(action, "не выбрано ни одного блока", "укажите --blocks или --preset")

    state = load_state(target)
    planned: List[Dict[str, Any]] = []
    dirs_planned: List[str] = []
    missing_placeholders: Dict[str, List[str]] = {}

    for block in blocks:
        for name in block.get("dirs", []):
            path = target / name
            if not path.exists():
                dirs_planned.append(name)

        for entry in block["files"]:
            src = root / block["path"] / entry["src"]
            rendered, missing = substitute(read_text(src), values)
            if missing:
                missing_placeholders[entry["dst"]] = sorted(missing)
            act, why = plan_file(target, state, block, entry, rendered)
            planned.append(
                {
                    "block": block["id"],
                    "src": f"{block['path']}/{entry['src']}",
                    "dst": entry["dst"],
                    "class": entry["class"],
                    "action": act,
                    "why": why,
                    "rendered": rendered,
                }
            )

    if missing_placeholders:
        for dst, names in sorted(missing_placeholders.items()):
            result.fail(
                "placeholder_missing",
                f"{dst}: не заданы значения для {names}",
                "передайте их через --set ИМЯ=значение или --values файл.json",
            )
        result.next = "Значения плейсхолдеров обязательны: файл с незакрытым маркером не ставится."
        return result.emit()

    if not do_apply:
        result.data = {
            "target": str(target),
            "blocks": [b["id"] for b in blocks],
            "dirs": dirs_planned,
            "files": [
                {k: v for k, v in item.items() if k != "rendered"} for item in planned
            ],
            "summary": summarize(planned),
        }
        result.ok("planned", describe(planned, dirs_planned))
        result.next = "Это сухой прогон, на диске ничего не изменилось. Повторите с --apply."
        return result.emit(EXIT_CONFIRM)

    target.mkdir(parents=True, exist_ok=True)
    for name in dirs_planned:
        (target / name).mkdir(parents=True, exist_ok=True)

    written: List[Dict[str, str]] = []
    for item in planned:
        dst = target / item["dst"]
        if item["action"] == ACTION_SKIP:
            continue
        if item["action"] == ACTION_SIDECAR:
            sidecar = dst.with_name(dst.name + ".new")
            write_text_atomic(sidecar, item["rendered"])
            written.append({"path": item["dst"] + ".new", "action": ACTION_SIDECAR})
            continue
        write_text_atomic(dst, item["rendered"])
        state.setdefault("installed", {})[item["dst"]] = {
            "block": item["block"],
            "class": item["class"],
            "src": item["src"],
            "sha256": sha256_text(item["rendered"]),
            "installed_at": now_iso(),
        }
        written.append({"path": item["dst"], "action": item["action"]})

    for block in blocks:
        state.setdefault("blocks", {})[block["id"]] = {
            "installed_at": now_iso(),
            "title": block["title"],
        }
    state["schema_version"] = STATE_SCHEMA
    write_json_atomic(state_path(target), state)

    result.data = {
        "target": str(target),
        "blocks": [b["id"] for b in blocks],
        "dirs": dirs_planned,
        "written": written,
        "summary": summarize(planned),
    }
    result.ok("applied", describe(planned, dirs_planned))
    sidecars = [w["path"] for w in written if w["action"] == ACTION_SIDECAR]
    if sidecars:
        result.warn(
            "sidecars",
            "рядом положены новые версии, ваши файлы не тронуты: " + ", ".join(sidecars),
            "сравните и перенесите нужное вручную, потом удалите .new",
        )
    result.next = "Проверьте результат: python bootstrap/check.py --project " + str(target)
    return result.emit()


def summarize(planned: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in planned:
        out[item["action"]] = out.get(item["action"], 0) + 1
    return out


def describe(planned: List[Dict[str, Any]], dirs: List[str]) -> str:
    counts = summarize(planned)
    parts = []
    labels = {
        ACTION_CREATE: "создать",
        ACTION_UPDATE: "обновить",
        ACTION_SKIP: "пропустить",
        ACTION_SIDECAR: "положить рядом .new",
    }
    for key, label in labels.items():
        if counts.get(key):
            parts.append(f"{label}: {counts[key]}")
    if dirs:
        parts.append(f"папок: {len(dirs)}")
    return "; ".join(parts) if parts else "делать нечего"


def parse_values(pairs: List[str], values_file: Optional[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if values_file:
        values.update({str(k): str(v) for k, v in read_json(Path(values_file)).items()})
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set ожидает ИМЯ=значение, получено: {pair}")
        name, value = pair.split("=", 1)
        values[name.strip()] = value
    return values


def main(argv: Optional[List[str]] = None) -> int:
    setup_io()
    parser = argparse.ArgumentParser(description="Разворачивание блоков в проект")
    parser.add_argument("--target", required=True, help="папка проекта (вне этого репозитория)")
    parser.add_argument("--plan", help="файл плана от select.py")
    parser.add_argument("--blocks", default="", help="список id блоков через запятую")
    parser.add_argument("--preset", help="имя пресета")
    parser.add_argument("--set", action="append", default=[], help="ИМЯ=значение для плейсхолдера")
    parser.add_argument("--values", help="JSON-файл со значениями плейсхолдеров")
    parser.add_argument("--apply", action="store_true", help="записать на диск (иначе сухой прогон)")
    args = parser.parse_args(argv)

    try:
        values = parse_values(args.set, args.values)
    except (ValueError, OSError) as exc:
        return die("apply", str(exc))

    ids = [b.strip() for b in args.blocks.split(",") if b.strip()]
    if args.plan:
        # План от select.py — обычный путь. Он же несёт значения плейсхолдеров,
        # собранные на брифе, чтобы их не приходилось перечислять руками.
        try:
            plan_data = read_json(Path(args.plan))
        except (OSError, ValueError) as exc:
            return die("apply", f"не читается план {args.plan}: {exc}")
        ids.extend(plan_data.get("blocks", []))
        for name, value in (plan_data.get("values") or {}).items():
            values.setdefault(str(name), str(value))

    return run(
        root=repo_root(),
        target=Path(args.target).expanduser(),
        block_ids=ids,
        preset=args.preset,
        values=values,
        do_apply=args.apply,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_USER)
