"""Каталог не расходится с файловой системой."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bootstrap"))

import catalog as cat  # noqa: E402
from _common import Result, find_placeholders, read_text  # noqa: E402


def run_script(*args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bootstrap" / "catalog.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, json.loads(proc.stdout)


class TestCatalogIsInSync(unittest.TestCase):
    def test_validate_passes(self):
        """Если этот тест красный — забыли пересобрать catalog.json."""
        code, payload = run_script("--validate")
        failures = [c for c in payload["checks"] if c["status"] == "fail"]
        self.assertEqual(failures, [], f"catalog.json разошёлся: {failures}")
        self.assertEqual(code, 0)

    def test_stdout_is_pure_json(self):
        """Скилл разбирает вывод машинно — ничего лишнего в stdout быть не может."""
        code, payload = run_script("--list")
        self.assertIn("checks", payload)
        self.assertIn("exit_code", payload)


class TestCatalogRules(unittest.TestCase):
    def setUp(self):
        self.catalog = cat.build(ROOT)

    def test_every_block_id_matches_its_path(self):
        for block in self.catalog["blocks"]:
            family, name = block["id"].split("/")
            self.assertEqual(block["path"], f"blocks/{family}/{name}")

    def test_ids_are_lowercase(self):
        """Linux различает регистр, Windows и macOS — нет. Разойдётся только в CI."""
        for block in self.catalog["blocks"]:
            self.assertEqual(block["id"], block["id"].lower(), block["id"])

    def test_no_two_blocks_claim_one_path(self):
        result = Result(action="t")
        cat.check_conflicts(self.catalog, result)
        self.assertEqual([c.detail for c in result.checks if c.status == "fail"], [])

    def test_no_poisonous_templates(self):
        """CLAUDE.md или .gitignore без .tpl подействовали бы на сам репозиторий."""
        result = Result(action="t")
        cat.check_poison(ROOT, result)
        self.assertEqual([c.detail for c in result.checks if c.status == "fail"], [])

    def test_requirements_exist(self):
        result = Result(action="t")
        cat.check_requires(self.catalog, result)
        self.assertEqual([c.detail for c in result.checks if c.status == "fail"], [])

    def test_placeholders_declared(self):
        result = Result(action="t")
        cat.check_placeholders(ROOT, self.catalog, result)
        self.assertEqual([c.detail for c in result.checks if c.status == "fail"], [])

    def test_file_classes_are_known(self):
        for block in self.catalog["blocks"]:
            for entry in block["files"]:
                self.assertIn(entry["class"], cat.FILE_CLASSES)

    def test_paths_use_forward_slashes(self):
        for block in self.catalog["blocks"]:
            for entry in block["files"]:
                self.assertNotIn("\\", entry["src"])
                self.assertNotIn("\\", entry["dst"])

    def test_every_block_has_summary_and_why(self):
        """Блок без объяснения «зачем» нельзя честно предложить или отвергнуть."""
        for block in self.catalog["blocks"]:
            self.assertTrue(block.get("summary"), block["id"])
            self.assertTrue(block.get("why"), block["id"])


class TestPriorities(unittest.TestCase):
    def test_priorities_template_ships_empty(self):
        """Ключевое правило доклада: файл приоритетов не заполняется заранее.

        Строка добавляется только после того, как агент реально ошибся, поэтому
        в шаблоне не должно быть готовых пунктов — только заголовки и подсказки
        в комментариях.
        """
        text = read_text(ROOT / "blocks/core/project-files/files/PRIORITIES.md.tpl")
        body_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("- ") and not line.strip().startswith("- <!--")
        ]
        self.assertEqual(body_lines, [], f"в шаблоне приоритетов есть готовые пункты: {body_lines}")


if __name__ == "__main__":
    unittest.main()
