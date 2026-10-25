#!/usr/bin/env python3
"""Выбор формы: один скилл, агент со скиллами, оркестратор или этап-сессия.

    python tools/choose_topology.py --answers answers.json
    python tools/choose_topology.py --stages 3 --volume large --needs-review
    python tools/choose_topology.py --answers answers.json --want 2

Форма считается кодом, а не выбирается моделью, по одной причине: это то место,
где по умолчанию делается красиво и дорого. Правило, которое можно уговорить,
правилом не является — поэтому `--want` выше вычисленного не поднимает форму, а
получает отказ с указанием, какой именно сигнал не сработал.

Направление подъёма всегда снизу вверх. «Начнём с красивой архитектуры, упростим
если что» на практике не происходит никогда.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

TOPOLOGY_PATH = Path(__file__).resolve().parent.parent / "references" / "topology.json"


def load_topology(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"файл {path} не найден — он ставится вместе с этим скриптом")
    return json.loads(path.read_text(encoding="utf-8"))


def value_for(signal: dict, answers: dict):
    """Достаёт значение сигнала из ответов. Особый случай — число этапов."""
    if signal["id"] == "stages_count":
        stages = answers.get("stages")
        if isinstance(stages, list):
            return len(stages)
        if isinstance(stages, int):
            return stages
        return None
    if signal["id"] == "intermediate_volume":
        return answers.get("volume")
    return answers.get(signal["id"])


def fires(signal: dict, value) -> bool:
    """Сработал ли сигнал. None означает «не спросили» — сигнал не срабатывает.

    Умолчание в пользу низшей формы намеренное: неизвестность не должна
    удорожать конструкцию.
    """
    if value is None:
        return False
    threshold = signal["threshold"]
    if isinstance(threshold, bool):
        return bool(value) is threshold
    if isinstance(threshold, int):
        return isinstance(value, int) and value >= threshold
    return value == threshold


def decide(topology: dict, answers: dict) -> dict:
    level = 0
    fired: list[dict] = []
    silent: list[dict] = []
    reviewer = False
    contract = False

    for signal in topology["signals"]:
        value = value_for(signal, answers)
        if fires(signal, value):
            fired.append({"id": signal["id"], "value": value, "why": signal["why"]})
            if "raises_to" in signal:
                level = max(level, signal["raises_to"])
            reviewer = reviewer or bool(signal.get("adds_reviewer"))
            contract = contract or bool(signal.get("requires_contract"))
        else:
            silent.append({"id": signal["id"], "value": value})

    chosen = next(lvl for lvl in topology["levels"] if lvl["id"] == level)
    return {
        "level": level,
        "name": chosen["name"],
        "title": chosen["title"],
        "cost": chosen["cost"],
        "generates": chosen["generates"],
        "fired": fired,
        "silent": silent,
        "needs_reviewer": reviewer,
        "needs_contract": contract,
    }


def refusal(topology: dict, decision: dict, wanted: int) -> dict:
    """Объясняет отказ поднять форму: какие сигналы для неё нужны и не сработали."""
    needed = [
        s for s in topology["signals"]
        if s.get("raises_to", 0) >= wanted
        and s["id"] not in {f["id"] for f in decision["fired"]}
    ]
    guard = next(g for g in topology["guards"] if g["id"] == "cheapest_that_works")
    return {
        "requested": wanted,
        "granted": decision["level"],
        "rule": guard["rule"],
        "why": guard["why"],
        "missing_signals": [
            {"id": s["id"], "ask": s["ask"], "why": s["why"]} for s in needed
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Выбор формы агентной системы")
    parser.add_argument("--answers", help="JSON-файл с ответами брифа")
    parser.add_argument("--stages", type=int, help="сколько этапов в задаче")
    parser.add_argument("--volume", choices=["small", "large"], help="объём данных этапа")
    parser.add_argument("--outputs-only", action="store_true",
                        help="следующему шагу достаточно выводов, сырьё не нужно")
    parser.add_argument("--independent", action="store_true", help="шаги независимы")
    parser.add_argument("--needs-review", action="store_true", help="нужна проверка свежим взглядом")
    parser.add_argument("--wont-fit", action="store_true", help="задача не влезает в один разговор")
    parser.add_argument("--handoff", action="store_true", help="результат уходит другому специалисту")
    parser.add_argument("--want", type=int, choices=[0, 1, 2, 3], help="желаемая форма")
    parser.add_argument("--topology", default=str(TOPOLOGY_PATH), help="путь к topology.json")
    args = parser.parse_args(argv)

    try:
        topology = load_topology(Path(args.topology))
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    answers: dict = {}
    if args.answers:
        try:
            answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": f"не читается {args.answers}: {exc}"},
                             ensure_ascii=False, indent=2))
            return 1

    if args.stages is not None:
        answers["stages"] = args.stages
    if args.volume:
        answers["volume"] = args.volume
    if args.outputs_only:
        answers["intermediate_needed_whole"] = False
    if args.independent:
        answers["independent_subtasks"] = True
    if args.needs_review:
        answers["needs_review"] = True
    if args.wont_fit:
        answers["fits_one_context"] = False
    if args.handoff:
        answers["handoff_between_people"] = True

    decision = decide(topology, answers)
    wanted = args.want if args.want is not None else answers.get("requested_level")

    payload = {
        "ok": True,
        "action": "topology.decide",
        "data": {"decision": decision},
        "exit_code": 0,
    }

    if wanted is not None and wanted > decision["level"]:
        payload["ok"] = False
        payload["exit_code"] = 2
        payload["data"]["refusal"] = refusal(topology, decision, wanted)
        payload["next"] = (
            f"Запрошена форма {wanted}, обоснована {decision['level']}. "
            "Либо остаёмся на обоснованной, либо назовите сработавший сигнал из списка."
        )
    else:
        payload["next"] = f"Форма {decision['level']}: {decision['title']}"

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
