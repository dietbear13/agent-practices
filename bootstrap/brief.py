"""Бриф: вопросы как данные, проверка ответов, сохранение результата.

    python bootstrap/brief.py --questions project
    python bootstrap/brief.py --validate answers.json
    python bootstrap/brief.py --save answers.json --project ../my-project
    python bootstrap/brief.py --gates

Вопросы лежат в brief/*.json и берутся оттуда, а не сочиняются на ходу. Причина
не в аккуратности: число обязательных вопросов до первого работающего результата
ограничено сознательно, и ограничение должно быть проверяемым, а не зависеть от
того, насколько разговорчив агент в этой сессии.

Скилл, который ведёт бриф, задаёт вопросы по одному и своими словами. Сырой JSON
человеку не показывается никогда.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    EXIT_CONFIRM,
    EXIT_USER,
    Result,
    die,
    read_json,
    repo_root,
    setup_io,
    write_json_atomic,
    write_text_atomic,
)

BRIEF_DIR = "brief"


def load_map(root: Path) -> Dict[str, Any]:
    return read_json(root / BRIEF_DIR / "00-map.json")


def load_brief(root: Path, brief_id: str) -> Dict[str, Any]:
    catalog = load_map(root)
    for entry in catalog["briefs"]:
        if entry["id"] == brief_id:
            return read_json(root / BRIEF_DIR / entry["file"])
    raise KeyError(brief_id)


def iter_questions(brief: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Вопросы брифа, включая ветвящиеся (04-reference хранит их по веткам)."""
    if "questions" in brief:
        return list(brief["questions"])
    out: List[Dict[str, Any]] = []
    for branch in brief.get("branches", {}).values():
        out.extend(branch.get("questions", []))
    return out


def all_required(root: Path) -> List[Dict[str, str]]:
    catalog = load_map(root)
    required = []
    for entry in catalog["briefs"]:
        brief = read_json(root / BRIEF_DIR / entry["file"])
        for question in iter_questions(brief):
            if question.get("required"):
                required.append(
                    {
                        "brief": entry["id"],
                        "id": question["id"],
                        "field": question["field"],
                        "ask": question["ask"],
                    }
                )
    return required


def get_field(answers: Dict[str, Any], field: str) -> Any:
    """Достаёт значение по пути вида 'task.output_format'."""
    node: Any = answers
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def is_answered(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def check_gates(root: Path, answers: Dict[str, Any], result: Result) -> None:
    """Гейты, которые не подлежат сокращению."""
    frequency = get_field(answers, "task.frequency")
    output_format = get_field(answers, "task.output_format")

    if frequency == "regular" and not is_answered(output_format):
        result.fail(
            "gate_output_format",
            "задача заявлена регулярной, но точного формата результата нет — "
            "регулярной она пока не является",
            "перечислите колонки или структуру, либо признайте задачу разовой",
        )
    elif frequency == "one_off" and not is_answered(output_format):
        result.info(
            "gate_output_format",
            "разовая задача без фиксированного формата — это нормально",
        )
    else:
        result.ok("gate_output_format", "формат результата зафиксирован")

    sources = get_field(answers, "task.sources") or []
    services = get_field(answers, "access.services") or {}
    missing = [s for s in sources if services.get(s) == "no"]
    unknown = [s for s in sources if services.get(s) == "unknown"]
    if missing:
        result.fail(
            "gate_service_access",
            f"задача опирается на сервисы, доступа к которым нет: {', '.join(missing)}",
            "предложите альтернативу из реестра или отложите задачу до появления доступа",
        )
    elif unknown:
        result.warn(
            "gate_service_access",
            f"по сервисам {', '.join(unknown)} доступ не выяснен — спросим при первом обращении",
        )
    else:
        result.ok("gate_service_access", "все нужные сервисы доступны")

    assumptions = get_field(answers, "assumptions") or {}
    confirmed = bool(assumptions.get("confirmed"))
    items = assumptions.get("items") or []
    if not confirmed:
        result.fail(
            "gate_gray_zones",
            "список допущений не подтверждён человеком",
            "перечислите, что приняли по умолчанию и что сломается, если это неверно, "
            "и дождитесь подтверждения",
        )
    elif not items:
        result.fail(
            "gate_gray_zones",
            "допущения подтверждены, но список пуст — подтверждение пустоты не является подтверждением",
            "серые зоны есть всегда: назовите хотя бы те, что приняли молча",
        )
    else:
        result.ok("gate_gray_zones", f"допущения подтверждены, пунктов: {len(items)}")


def opened_briefs(root: Path, answers: Dict[str, Any]) -> List[str]:
    opened: List[str] = []
    if (get_field(answers, "task.stages_count") or 1) >= 2:
        opened.append("shape")
    if get_field(answers, "task.done_before") is not None:
        opened.append("reference")
    if is_answered(get_field(answers, "access.external")):
        opened.append("adapt")
    return opened


def cmd_questions(root: Path, brief_id: str) -> int:
    try:
        brief = load_brief(root, brief_id)
    except KeyError:
        ids = [b["id"] for b in load_map(root)["briefs"]]
        return die("brief.questions", f"брифа {brief_id} нет", f"есть: {', '.join(ids)}")
    questions = iter_questions(brief)
    result = Result(action="brief.questions")
    result.data = {
        "brief": brief_id,
        "title": brief["title"],
        "required": [q for q in questions if q.get("required")],
        "optional": [q for q in questions if not q.get("required")],
    }
    result.ok(
        "loaded",
        f"{brief['title']}: обязательных {len(result.data['required'])}, "
        f"остальных {len(result.data['optional'])}",
    )
    result.next = "Задавайте по одному вопросу и своими словами. JSON человеку не показывайте."
    return result.emit()


def cmd_validate(root: Path, answers_path: Path) -> int:
    result = Result(action="brief.validate")
    try:
        answers = read_json(answers_path)
    except (OSError, ValueError) as exc:
        return die("brief.validate", f"не читается {answers_path}: {exc}")

    missing = [
        q for q in all_required(root) if not is_answered(get_field(answers, q["field"]))
    ]
    if missing:
        for q in missing:
            result.fail("required_missing", f"{q['field']}: {q['ask']}")
    else:
        result.ok("required_complete", "обязательные вопросы закрыты")

    check_gates(root, answers, result)

    result.data = {
        "missing_required": [q["field"] for q in missing],
        "opened_briefs": opened_briefs(root, answers),
        "deferred": get_field(answers, "unresolved") or [],
    }
    if result.has_failures:
        result.next = "Разворачивать нельзя: сначала закройте перечисленное."
        return result.emit(EXIT_CONFIRM)
    result.next = "Бриф готов. Дальше: python bootstrap/select.py --brief " + str(answers_path)
    return result.emit()


def render_markdown(answers: Dict[str, Any]) -> str:
    """Человекочитаемый слепок брифа — чтобы решения можно было пересмотреть глазами."""
    lines = ["# Бриф", ""]
    lines.append("Ответы, из которых собрана система. Правится руками; чтобы изменения")
    lines.append("вступили в силу, пересоберите план.")
    lines.append("")

    titles = {
        "project": "Проект",
        "task": "Задача",
        "access": "Доступы",
        "shape": "Форма",
        "reference": "Эталон",
        "adapt": "Адаптация чужого",
        "assumptions": "Допущения",
    }
    for key, title in titles.items():
        section = answers.get(key)
        if not section:
            continue
        lines.append(f"## {title}")
        lines.append("")
        if isinstance(section, dict):
            for name, value in section.items():
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value) if value else "—"
                elif isinstance(value, dict):
                    value = ", ".join(f"{k}: {v}" for k, v in value.items()) or "—"
                elif isinstance(value, bool):
                    value = "да" if value else "нет"
                lines.append(f"- **{name}**: {value}")
        else:
            lines.append(f"- {section}")
        lines.append("")
    return "\n".join(lines) + "\n"


def cmd_save(root: Path, answers_path: Path, project: Path) -> int:
    result = Result(action="brief.save")
    try:
        answers = read_json(answers_path)
    except (OSError, ValueError) as exc:
        return die("brief.save", f"не читается {answers_path}: {exc}")

    brief_file = project / ".agent-kit" / "brief.json"
    write_json_atomic(brief_file, answers)
    md_file = project / "BRIEF.md"
    write_text_atomic(md_file, render_markdown(answers))

    result.ok("saved", "бриф сохранён: машинный в .agent-kit/brief.json, читаемый в BRIEF.md")
    result.data = {"brief": str(brief_file), "markdown": str(md_file)}
    result.next = "Доустановка потом прочитает этот файл вместо повторного опроса."
    return result.emit()


def cmd_gates(root: Path) -> int:
    result = Result(action="brief.gates")
    catalog = load_map(root)
    result.data = {"gates": catalog["gates"], "max_required": catalog["max_required_total"]}
    result.ok("loaded", f"гейтов: {len(catalog['gates'])}")
    return result.emit()


def main(argv: Optional[List[str]] = None) -> int:
    setup_io()
    parser = argparse.ArgumentParser(description="Бриф конструктора")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--questions", metavar="BRIEF", help="вопросы одного брифа")
    group.add_argument("--validate", metavar="FILE", help="проверить ответы")
    group.add_argument("--save", metavar="FILE", help="сохранить ответы в проект")
    group.add_argument("--gates", action="store_true", help="показать гейты")
    group.add_argument("--required", action="store_true", help="все обязательные вопросы")
    parser.add_argument("--project", help="папка проекта (для --save)")
    args = parser.parse_args(argv)

    root = repo_root()
    if args.questions:
        return cmd_questions(root, args.questions)
    if args.validate:
        return cmd_validate(root, Path(args.validate))
    if args.save:
        if not args.project:
            return die("brief.save", "не указана папка проекта", "добавьте --project ПУТЬ")
        return cmd_save(root, Path(args.save), Path(args.project).expanduser())
    if args.gates:
        return cmd_gates(root)

    result = Result(action="brief.required")
    required = all_required(root)
    result.data = {"required": required, "count": len(required)}
    result.ok("loaded", f"обязательных вопросов всего: {len(required)}")
    return result.emit()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_USER)
