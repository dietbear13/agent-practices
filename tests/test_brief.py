"""Бриф: ограничение на число вопросов, гейты, превращение ответов в блоки."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bootstrap"))

import brief as b  # noqa: E402
from _common import read_json  # noqa: E402

BRIEF = ROOT / "bootstrap" / "brief.py"
SELECT = ROOT / "bootstrap" / "select.py"


def run(script: Path, *args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, encoding="utf-8",
    )
    return proc.returncode, json.loads(proc.stdout)


GOOD = {
    "project": {"path": "../demo", "name": "example.ru"},
    "task": {
        "name": "Недельный отчёт по трафику",
        "frequency": "regular",
        "output_format": "CSV, колонки: дата, источник, визиты",
        "acceptance": "сумма визитов сходится с интерфейсом Метрики",
        "sources": ["yandex_metrika"],
        "stages_count": 2,
    },
    "access": {"services": {"yandex_metrika": "yes"}},
    "assumptions": {
        "confirmed": True,
        "items": [{"assumption": "атрибуция — последний переход",
                   "breaks_if_wrong": "цифры не сойдутся с отчётом клиента"}],
    },
}


def written(answers: dict, tmp: str, name: str = "brief.json") -> Path:
    path = Path(tmp) / name
    path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return path


class TestRequiredBudget(unittest.TestCase):
    def test_no_more_than_declared(self):
        """Ограничение на обязательные вопросы — проверяемое, а не декларация.

        Длинный опрос до первого результата — главная причина, по которой такие
        мастера бросают на середине.
        """
        limit = b.load_map(ROOT)["max_required_total"]
        required = b.all_required(ROOT)
        self.assertLessEqual(
            len(required), limit,
            f"обязательных {len(required)} при потолке {limit}: "
            + ", ".join(q["field"] for q in required),
        )

    def test_every_question_explains_itself(self):
        """У каждого вопроса есть «зачем» — иначе на него нечего ответить кроме «не знаю»."""
        for entry in b.load_map(ROOT)["briefs"]:
            data = read_json(ROOT / "brief" / entry["file"])
            for q in b.iter_questions(data):
                self.assertTrue(q.get("ask"), f"{entry['id']}.{q['id']}: нет вопроса")
                self.assertTrue(q.get("why"), f"{entry['id']}.{q['id']}: нет объяснения why")

    def test_optional_questions_have_defaults(self):
        """Необязательный вопрос без умолчания на деле обязательный."""
        for entry in b.load_map(ROOT)["briefs"]:
            data = read_json(ROOT / "brief" / entry["file"])
            for q in b.iter_questions(data):
                if not q.get("required"):
                    self.assertIn("default", q, f"{entry['id']}.{q['id']}: нет default")


class TestSelectsBlocksExist(unittest.TestCase):
    def test_all_referenced_blocks_are_in_catalog(self):
        """Бриф не может включать блок, которого нет: это ломает сборку у человека."""
        catalog = read_json(ROOT / "catalog.json")
        known = {block["id"] for block in catalog["blocks"]}
        referenced = set()
        for entry in b.load_map(ROOT)["briefs"]:
            data = read_json(ROOT / "brief" / entry["file"])
            for q in b.iter_questions(data):
                selects = q.get("selects_blocks") or []
                if isinstance(selects, dict):
                    for ids in selects.values():
                        referenced.update(ids)
                else:
                    referenced.update(selects)
        self.assertEqual(referenced - known, set(), "брифы ссылаются на несуществующие блоки")


class TestGates(unittest.TestCase):
    def failures(self, payload: dict) -> list[str]:
        return [c["name"] for c in payload["checks"] if c["status"] == "fail"]

    def test_good_brief_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = run(BRIEF, "--validate", str(written(GOOD, tmp)))
            self.assertEqual(self.failures(payload), [])
            self.assertEqual(code, 0)

    def test_regular_task_without_format_is_stopped(self):
        answers = json.loads(json.dumps(GOOD))
        answers["task"]["output_format"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertIn("gate_output_format", self.failures(payload))
            self.assertEqual(code, 2, "остановка по правилу, а не ошибка")

    def test_one_off_without_format_is_fine(self):
        answers = json.loads(json.dumps(GOOD))
        answers["task"]["frequency"] = "one_off"
        answers["task"]["output_format"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertNotIn("gate_output_format", self.failures(payload))

    def test_task_needing_missing_service_is_stopped(self):
        answers = json.loads(json.dumps(GOOD))
        answers["task"]["sources"] = ["google_search_console"]
        answers["access"]["services"]["google_search_console"] = "no"
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertIn("gate_service_access", self.failures(payload))

    def test_unknown_access_is_a_warning_not_a_stop(self):
        answers = json.loads(json.dumps(GOOD))
        answers["task"]["sources"] = ["topvisor"]
        answers["access"]["services"]["topvisor"] = "unknown"
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertNotIn("gate_service_access", self.failures(payload))

    def test_unconfirmed_assumptions_stop_everything(self):
        answers = json.loads(json.dumps(GOOD))
        answers["assumptions"]["confirmed"] = False
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertIn("gate_gray_zones", self.failures(payload))

    def test_empty_assumption_list_is_not_confirmation(self):
        """Подтверждение пустоты — не подтверждение: серые зоны есть всегда."""
        answers = json.loads(json.dumps(GOOD))
        answers["assumptions"] = {"confirmed": True, "items": []}
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertIn("gate_gray_zones", self.failures(payload))

    def test_missing_required_is_reported_by_field(self):
        answers = json.loads(json.dumps(GOOD))
        del answers["task"]["acceptance"]
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertIn("task.acceptance", payload["data"]["missing_required"])


class TestConditionalBriefs(unittest.TestCase):
    def test_two_stages_open_shape_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(GOOD, tmp)))
            self.assertIn("shape", payload["data"]["opened_briefs"])

    def test_single_stage_does_not(self):
        answers = json.loads(json.dumps(GOOD))
        answers["task"]["stages_count"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = run(BRIEF, "--validate", str(written(answers, tmp)))
            self.assertNotIn("shape", payload["data"]["opened_briefs"])


class TestSelect(unittest.TestCase):
    def plan_blocks(self, answers: dict, tmp: str, *extra: str) -> dict:
        path = written(answers, tmp)
        code, payload = run(SELECT, "--brief", str(path),
                            "--out", str(Path(tmp) / "plan.json"), *extra)
        self.assertEqual(code, 0, payload)
        return payload

    def test_regular_task_gets_runtime_not_task_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.plan_blocks(GOOD, tmp)
            blocks = payload["data"]["blocks"]
            self.assertIn("core/script-runtime", blocks)
            self.assertNotIn("core/task-folder", blocks)

    def test_one_off_task_gets_task_folder_not_runtime(self):
        answers = json.loads(json.dumps(GOOD))
        answers["task"]["frequency"] = "one_off"
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.plan_blocks(answers, tmp)
            blocks = payload["data"]["blocks"]
            self.assertIn("core/task-folder", blocks)
            self.assertNotIn("core/script-runtime", blocks)

    def test_dependencies_are_pulled_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.plan_blocks(GOOD, tmp)
            self.assertIn("core/project-files", payload["data"]["blocks"])

    def test_every_block_carries_a_reason(self):
        """Блок без обоснования нельзя ни принять, ни осмысленно отвергнуть."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.plan_blocks(GOOD, tmp)
            for item in payload["data"]["plan"]:
                self.assertTrue(item["reason"], item["block"])
                self.assertTrue(item["source"], item["block"])

    def test_skipped_blocks_are_listed(self):
        """Человек должен видеть, что система знает про блок и не ставит его сознательно."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.plan_blocks(GOOD, tmp)
            self.assertTrue(payload["data"]["skipped"])

    def test_false_answer_does_not_select_block(self):
        answers = json.loads(json.dumps(GOOD))
        answers["project"]["dossier"] = False
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.plan_blocks(answers, tmp)
            self.assertNotIn("core/dossier", payload["data"]["blocks"])

    def test_true_answer_selects_block(self):
        answers = json.loads(json.dumps(GOOD))
        answers["project"]["dossier"] = True
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.plan_blocks(answers, tmp)
            self.assertIn("core/dossier", payload["data"]["blocks"])


class TestBriefSave(unittest.TestCase):
    def test_saves_machine_and_human_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            code, _ = run(BRIEF, "--save", str(written(GOOD, tmp)), "--project", str(project))
            self.assertEqual(code, 0)
            self.assertTrue((project / ".agent-kit" / "brief.json").is_file())
            markdown = (project / "BRIEF.md").read_text(encoding="utf-8")
            self.assertIn("Недельный отчёт", markdown)


if __name__ == "__main__":
    unittest.main()
