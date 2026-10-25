"""Каталог блоков: сборка, проверка и выдача.

`catalog.json` в корне репозитория не пишется руками — он собирается отсюда из
`blocks/*/*/block.json`. Причина простая: два источника правды расходятся всегда,
вопрос только в том, через сколько недель это заметят.

Хеши в каталоге считаются от файлов-исходников. Они защищают от другого сбоя,
чем `state.json` у пользователя: каталог отвечает на вопрос «этот файл дошёл до
человека таким, каким лежит в репозитории», а состояние проекта — на вопрос
«человек правил этот файл после установки».

    python bootstrap/catalog.py --build
    python bootstrap/catalog.py --validate
    python bootstrap/catalog.py --list [--step N] [--preset NAME]
    python bootstrap/catalog.py --block core/project-files
    python bootstrap/catalog.py --preset minimal
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EXIT_OK,
    EXIT_USER,
    Result,
    die,
    read_json,
    repo_root,
    setup_io,
    sha256_file,
    write_json_atomic,
)

SCHEMA_VERSION = 1

FILE_CLASSES = {"generated", "owned"}

# Имена, которые нельзя класть в blocks/ без суффикса .tpl: Claude Code прочитает
# их при открытии самого репозитория, а git применит .gitignore к нему же.
POISONOUS_NAMES = {"CLAUDE.md", "AGENTS.md", ".gitignore", ".env"}


def block_dirs(root: Path) -> List[Path]:
    blocks = root / "blocks"
    if not blocks.is_dir():
        return []
    found = []
    for family in sorted(p for p in blocks.iterdir() if p.is_dir()):
        for block in sorted(p for p in family.iterdir() if p.is_dir()):
            if (block / "block.json").is_file():
                found.append(block)
    return found


def load_block(block_dir: Path, root: Path) -> Dict[str, Any]:
    meta = read_json(block_dir / "block.json")
    expected_id = f"{block_dir.parent.name}/{block_dir.name}"
    if meta.get("id") != expected_id:
        raise ValueError(
            f"{block_dir}: поле id = {meta.get('id')!r}, а по расположению должно быть {expected_id!r}"
        )
    files = []
    for entry in meta.get("files", []):
        src = block_dir / entry["src"]
        if not src.is_file():
            raise ValueError(f"{expected_id}: файла {entry['src']} нет на диске")
        if entry.get("class") not in FILE_CLASSES:
            raise ValueError(
                f"{expected_id}: у {entry['src']} класс {entry.get('class')!r}, "
                f"допустимы {sorted(FILE_CLASSES)}"
            )
        files.append(
            {
                "src": entry["src"],
                "dst": entry["dst"],
                "class": entry["class"],
                "note": entry.get("note", ""),
                "sha256": sha256_file(src),
            }
        )
    meta["files"] = files
    meta["path"] = f"blocks/{block_dir.parent.name}/{block_dir.name}"
    return meta


def build(root: Path) -> Dict[str, Any]:
    blocks = [load_block(d, root) for d in block_dirs(root)]
    presets: Dict[str, List[str]] = {}
    for block in blocks:
        for preset in block.get("presets", []):
            presets.setdefault(preset, []).append(block["id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "blocks": blocks,
        "presets": {name: sorted(ids) for name, ids in sorted(presets.items())},
    }


def check_conflicts(catalog: Dict[str, Any], result: Result) -> None:
    """Два блока не имеют права претендовать на один и тот же путь установки.

    Иначе порядок установки начинает решать, чей файл победит, — а он зависит от
    того, в каком порядке человек ответил на бриф.
    """
    claimed: Dict[str, str] = {}
    for block in catalog["blocks"]:
        for entry in block["files"]:
            dst = entry["dst"]
            owner = claimed.get(dst)
            if owner is not None and owner != block["id"]:
                result.fail(
                    "file_conflict",
                    f"путь {dst} заявлен и блоком {owner}, и блоком {block['id']}",
                    "оставьте файл в одном блоке, второй пусть зависит от первого",
                )
            claimed[dst] = block["id"]


def check_poison(root: Path, result: Result) -> None:
    for path in (root / "blocks").rglob("*"):
        if path.is_file() and path.name in POISONOUS_NAMES:
            rel = path.relative_to(root).as_posix()
            result.fail(
                "poisonous_template",
                f"{rel} лежит без суффикса .tpl",
                "переименуйте в *.tpl — иначе файл действует внутри самого репозитория",
            )


def check_requires(catalog: Dict[str, Any], result: Result) -> None:
    known = {b["id"] for b in catalog["blocks"]}
    for block in catalog["blocks"]:
        for dep in block.get("requires", []):
            if dep not in known:
                result.fail(
                    "unknown_requirement",
                    f"{block['id']} требует {dep}, которого нет в каталоге",
                )


def check_placeholders(root: Path, catalog: Dict[str, Any], result: Result) -> None:
    """Всё, что встречается в файлах блока, должно быть объявлено в block.json."""
    from _common import find_placeholders, read_text

    for block in catalog["blocks"]:
        declared = set(block.get("placeholders", []))
        used = set()
        for entry in block["files"]:
            src = root / block["path"] / entry["src"]
            used |= find_placeholders(read_text(src))
        undeclared = used - declared
        if undeclared:
            result.fail(
                "undeclared_placeholder",
                f"{block['id']}: в файлах есть {sorted(undeclared)}, но их нет в placeholders",
                "допишите имена в block.json — иначе подстановка их пропустит",
            )
        unused = declared - used
        if unused:
            result.warn(
                "unused_placeholder",
                f"{block['id']}: объявлены, но не встречаются: {sorted(unused)}",
            )


def cmd_build(root: Path) -> int:
    result = Result(action="catalog.build")
    try:
        catalog = build(root)
    except ValueError as exc:
        return die("catalog.build", str(exc))
    check_conflicts(catalog, result)
    check_poison(root, result)
    check_requires(catalog, result)
    check_placeholders(root, catalog, result)
    if result.has_failures:
        result.next = "Каталог не собран: сначала исправьте ошибки выше."
        return result.emit()
    write_json_atomic(root / "catalog.json", catalog)
    result.ok("written", f"catalog.json собран: {len(catalog['blocks'])} блоков")
    result.data = {
        "blocks": len(catalog["blocks"]),
        "presets": {k: len(v) for k, v in catalog["presets"].items()},
    }
    result.next = "Каталог актуален."
    return result.emit()


def cmd_validate(root: Path) -> int:
    result = Result(action="catalog.validate")
    path = root / "catalog.json"
    if not path.is_file():
        return die(
            "catalog.validate",
            "catalog.json отсутствует",
            "соберите его: python bootstrap/catalog.py --build",
        )
    try:
        fresh = build(root)
    except ValueError as exc:
        return die("catalog.validate", str(exc))
    stored = read_json(path)
    if stored == fresh:
        result.ok("in_sync", "catalog.json совпадает с содержимым blocks/")
    else:
        result.fail(
            "out_of_sync",
            "catalog.json разошёлся с файлами блоков",
            "пересоберите: python bootstrap/catalog.py --build",
        )
    check_conflicts(fresh, result)
    check_poison(root, result)
    check_requires(fresh, result)
    check_placeholders(root, fresh, result)
    return result.emit()


def load_catalog(root: Path) -> Dict[str, Any]:
    return read_json(root / "catalog.json")


def cmd_list(root: Path, step: Optional[int], preset: Optional[str]) -> int:
    result = Result(action="catalog.list")
    catalog = load_catalog(root)
    blocks = catalog["blocks"]
    if step is not None:
        blocks = [b for b in blocks if b.get("step") == step]
    if preset is not None:
        ids = set(catalog["presets"].get(preset, []))
        blocks = [b for b in blocks if b["id"] in ids]
    result.data = {
        "blocks": [
            {
                "id": b["id"],
                "title": b["title"],
                "step": b.get("step"),
                "summary": b.get("summary", ""),
                "requires": b.get("requires", []),
                "files": len(b["files"]),
            }
            for b in blocks
        ]
    }
    result.ok("listed", f"найдено блоков: {len(blocks)}")
    return result.emit()


def cmd_block(root: Path, block_id: str) -> int:
    catalog = load_catalog(root)
    for block in catalog["blocks"]:
        if block["id"] == block_id:
            result = Result(action="catalog.block")
            result.data = {"block": block}
            result.ok("found", block["title"])
            return result.emit()
    return die(
        "catalog.block",
        f"блока {block_id} нет в каталоге",
        "список: python bootstrap/catalog.py --list",
    )


def cmd_preset(root: Path, name: str) -> int:
    catalog = load_catalog(root)
    ids = catalog["presets"].get(name)
    if ids is None:
        return die(
            "catalog.preset",
            f"пресета {name} нет",
            f"доступны: {', '.join(sorted(catalog['presets']))}",
        )
    result = Result(action="catalog.preset")
    result.data = {"preset": name, "blocks": ids}
    result.ok("found", f"{name}: блоков {len(ids)}")
    return result.emit()


def main(argv: Optional[List[str]] = None) -> int:
    setup_io()
    parser = argparse.ArgumentParser(description="Каталог блоков agent-kit")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true", help="собрать catalog.json из blocks/")
    group.add_argument("--validate", action="store_true", help="сверить catalog.json с blocks/")
    group.add_argument("--list", action="store_true", help="список блоков")
    group.add_argument("--block", metavar="ID", help="один блок целиком")
    group.add_argument("--preset", metavar="NAME", help="состав пресета")
    parser.add_argument("--step", type=int, help="фильтр по шагу доклада (с --list)")
    parser.add_argument("--in-preset", metavar="NAME", help="фильтр по пресету (с --list)")
    args = parser.parse_args(argv)

    root = repo_root()
    if args.build:
        return cmd_build(root)
    if args.validate:
        return cmd_validate(root)
    if args.list:
        return cmd_list(root, args.step, args.in_preset)
    if args.block:
        return cmd_block(root, args.block)
    return cmd_preset(root, args.preset)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_USER)
