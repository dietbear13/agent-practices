"""Бриф → список блоков, с обоснованием на каждый.

    python bootstrap/select.py --brief answers.json
    python bootstrap/select.py --brief answers.json --preset solo-seo
    python bootstrap/select.py --brief answers.json --add core/dossier --drop core/verify

Выбор блоков — не решение модели, а объединение множеств: у каждого вопроса в
брифе записано, какие блоки включает тот или иной ответ. Модель может отговорить
от блока или предложить добавить, но не может придумать блок, которого нет в
каталоге, и не может тихо доложить «на всякий случай» ещё пяток.

Отдельно показывается, что НЕ взято и почему. Это половина ценности: человек
должен видеть, что система знает про блок и сознательно его не ставит, — иначе
через неделю он поставит его сам «чтобы было».
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EXIT_USER,
    Result,
    die,
    read_json,
    repo_root,
    setup_io,
    write_json_atomic,
)

from brief import get_field, is_answered, iter_questions, load_map  # noqa: E402


def collect_from_briefs(root: Path, answers: Dict[str, Any]) -> List[Dict[str, str]]:
    """Проходит по всем вопросам и собирает блоки, которые включили ответы."""
    picked: List[Dict[str, str]] = []
    catalog = load_map(root)

    for entry in catalog["briefs"]:
        brief = read_json(root / "brief" / entry["file"])
        for question in iter_questions(brief):
            selects = question.get("selects_blocks")
            if not selects:
                continue
            value = get_field(answers, question["field"])
            if not is_answered(value):
                continue

            if isinstance(selects, dict):
                # Блоки зависят от конкретного ответа: «регулярная» и «разовая»
                # включают разное.
                key = str(value)
                for block_id in selects.get(key, []):
                    picked.append(
                        {
                            "block": block_id,
                            "reason": f"{question['ask']} → {key}",
                            "source": f"brief:{entry['id']}.{question['id']}",
                        }
                    )
            else:
                if value is False:
                    continue
                for block_id in selects:
                    picked.append(
                        {
                            "block": block_id,
                            "reason": question["ask"],
                            "source": f"brief:{entry['id']}.{question['id']}",
                        }
                    )
    return picked


def collect_values(root: Path, answers: Dict[str, Any]) -> Dict[str, str]:
    """Значения плейсхолдеров, собранные на брифе.

    У вопроса может стоять поле `placeholder` — тогда его ответ подставляется в
    шаблоны блоков. Так человек не вводит одно и то же дважды: имя проекта он уже
    называл в брифе.
    """
    values: Dict[str, str] = {}
    for entry in load_map(root)["briefs"]:
        brief = read_json(root / "brief" / entry["file"])
        for question in iter_questions(brief):
            name = question.get("placeholder")
            if not name:
                continue
            value = get_field(answers, question["field"])
            if is_answered(value):
                values[name] = str(value)
    return values


def resolve(
    catalog: Dict[str, Any], picked: List[Dict[str, str]]
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Дотягивает зависимости и убирает повторы, сохраняя первое обоснование."""
    by_id = {b["id"]: b for b in catalog["blocks"]}
    unknown = [p["block"] for p in picked if p["block"] not in by_id]
    if unknown:
        return [], sorted(set(unknown))

    plan: List[Dict[str, str]] = []
    seen = set()

    def add(block_id: str, reason: str, source: str) -> None:
        if block_id in seen:
            return
        for dep in by_id[block_id].get("requires", []):
            add(dep, f"нужен для «{by_id[block_id]['title']}»", "dependency")
        seen.add(block_id)
        plan.append(
            {
                "block": block_id,
                "title": by_id[block_id]["title"],
                "step": by_id[block_id].get("step"),
                "reason": reason,
                "source": source,
            }
        )

    for item in picked:
        add(item["block"], item["reason"], item["source"])
    return plan, []


def skipped(catalog: Dict[str, Any], chosen: set) -> List[Dict[str, str]]:
    return [
        {
            "block": b["id"],
            "title": b["title"],
            "why_not": b.get("summary", ""),
            "add_with": f"python bootstrap/select.py --add {b['id']}",
        }
        for b in catalog["blocks"]
        if b["id"] not in chosen
    ]


def count_skills(catalog: Dict[str, Any], chosen: set) -> int:
    """Скиллов у пользователя не должно становиться слишком много.

    Переполненный список инструментов не даёт ошибки: часть скиллов просто
    перестаёт быть видимой для модели, а скилл, которого нет в списке, для неё
    не существует.
    """
    total = 0
    for block in catalog["blocks"]:
        if block["id"] not in chosen:
            continue
        total += sum(1 for f in block["files"] if "/skills/" in f["dst"] or f["dst"].endswith("SKILL.md"))
    return total


SKILL_LIMIT = 10


def run(
    root: Path,
    brief_path: Path,
    preset: Optional[str],
    add: List[str],
    drop: List[str],
    out: Optional[Path],
) -> int:
    result = Result(action="select")
    catalog_path = root / "catalog.json"
    if not catalog_path.is_file():
        return die("select", "catalog.json отсутствует", "python bootstrap/catalog.py --build")
    catalog = read_json(catalog_path)

    try:
        answers = read_json(brief_path)
    except (OSError, ValueError) as exc:
        return die("select", f"не читается {brief_path}: {exc}")

    picked = collect_from_briefs(root, answers)

    if preset:
        if preset not in catalog["presets"]:
            return die(
                "select",
                f"пресета {preset} нет",
                f"есть: {', '.join(sorted(catalog['presets']))}",
            )
        for block_id in catalog["presets"][preset]:
            picked.append({"block": block_id, "reason": f"входит в пресет {preset}", "source": "preset"})

    for block_id in add:
        picked.append({"block": block_id, "reason": "добавлен вручную", "source": "manual"})

    plan, unknown = resolve(catalog, picked)
    if unknown:
        return die(
            "select",
            f"в брифе указаны блоки, которых нет в каталоге: {', '.join(unknown)}",
            "проверьте selects_blocks в brief/*.json",
        )

    dropped = []
    if drop:
        keep = []
        by_id = {b["id"]: b for b in catalog["blocks"]}
        required_by = {
            dep
            for item in plan
            for dep in by_id[item["block"]].get("requires", [])
        }
        for item in plan:
            if item["block"] in drop and item["block"] in required_by:
                result.warn(
                    "drop_refused",
                    f"{item['block']} нельзя убрать: от него зависят другие выбранные блоки",
                )
                keep.append(item)
            elif item["block"] in drop:
                dropped.append(item["block"])
            else:
                keep.append(item)
        plan = keep

    chosen = {item["block"] for item in plan}
    skills = count_skills(catalog, chosen)
    values = collect_values(root, answers)

    result.data = {
        "plan": plan,
        "skipped": skipped(catalog, chosen),
        "dropped": dropped,
        "blocks": sorted(chosen),
        "skill_count": skills,
        "values": values,
    }

    if not plan:
        result.fail(
            "empty_plan",
            "ни один блок не выбран",
            "проверьте, что бриф заполнен, или укажите --preset",
        )
        return result.emit()

    result.ok("planned", f"блоков: {len(plan)}, из них по зависимостям: "
                         f"{sum(1 for i in plan if i['source'] == 'dependency')}")

    by_id = {b["id"]: b for b in catalog["blocks"]}
    needed = {ph for bid in chosen for ph in by_id[bid].get("placeholders", [])}
    missing = sorted(needed - set(values))
    if missing:
        result.warn(
            "placeholder_unanswered",
            f"блокам нужны значения, которых нет в брифе: {', '.join(missing)}",
            "ответьте на соответствующие вопросы брифа или передайте --set при разворачивании",
        )

    if skills > SKILL_LIMIT:
        result.warn(
            "skill_budget",
            f"скиллов получается {skills} при ориентире {SKILL_LIMIT}",
            "уберите лишние блоки: переполненный список делает часть скиллов невидимой для модели",
        )

    target = out or (Path(answers.get("project", {}).get("path", ".")) / ".agent-kit" / "plan.json")
    write_json_atomic(target, {"blocks": sorted(chosen), "plan": plan, "values": values})
    result.data["written"] = str(target)
    result.next = (
        "Покажите человеку и что взяли, и что не взяли — с обоснованием. "
        "После подтверждения: python bootstrap/apply.py --plan " + str(target)
    )
    return result.emit()


def main(argv: Optional[List[str]] = None) -> int:
    setup_io()
    parser = argparse.ArgumentParser(description="Выбор блоков по брифу")
    parser.add_argument("--brief", required=True, help="файл с ответами брифа")
    parser.add_argument("--preset", help="добавить блоки пресета")
    parser.add_argument("--add", action="append", default=[], help="добавить блок вручную")
    parser.add_argument("--drop", action="append", default=[], help="убрать блок")
    parser.add_argument("--out", help="куда записать план")
    args = parser.parse_args(argv)

    return run(
        root=repo_root(),
        brief_path=Path(args.brief),
        preset=args.preset,
        add=args.add,
        drop=args.drop,
        out=Path(args.out) if args.out else None,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_USER)
