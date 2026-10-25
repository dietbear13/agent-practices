"""Разворачивание блоков: что создаётся, что пропускается, что не перезаписывается."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bootstrap"))

APPLY = ROOT / "bootstrap" / "apply.py"


def run_apply(*args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(APPLY), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, json.loads(proc.stdout)


class ApplyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "project"

    def tearDown(self):
        self._tmp.cleanup()

    def install(self, name: str = "example.ru", *extra: str) -> dict:
        code, payload = run_apply(
            "--target", str(self.target),
            "--preset", "minimal",
            "--set", f"PROJECT_NAME={name}",
            "--apply",
            *extra,
        )
        self.assertEqual(code, 0, payload)
        return payload

    def summary(self, payload: dict) -> dict:
        return payload["data"]["summary"]


class TestFirstInstall(ApplyCase):
    def test_dry_run_writes_nothing(self):
        code, payload = run_apply(
            "--target", str(self.target),
            "--preset", "minimal",
            "--set", "PROJECT_NAME=example.ru",
        )
        self.assertEqual(code, 2, "сухой прогон возвращает 2 — нужно подтверждение человека")
        self.assertFalse(self.target.exists(), "сухой прогон создал файлы на диске")

    def test_creates_expected_tree(self):
        self.install()
        for name in ("CLAUDE.md", "PRIORITIES.md", ".gitignore", ".env.example"):
            self.assertTrue((self.target / name).is_file(), name)
        for name in ("skills", "tools", "outputs", "tasks", "temp"):
            self.assertTrue((self.target / name).is_dir(), name)

    def test_placeholder_substituted(self):
        self.install()
        self.assertIn("example.ru", (self.target / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_no_placeholders_left(self):
        self.install()
        for path in self.target.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".example"}:
                self.assertNotIn("<<<", path.read_text(encoding="utf-8"), str(path))

    def test_no_service_state_in_project(self):
        """В проект пользователя попадают только файлы блоков, без служебного каталога."""
        self.install()
        self.assertFalse((self.target / ".agent-kit").exists())

    def test_gitignore_closes_secrets_and_one_off_work(self):
        self.install()
        text = (self.target / ".gitignore").read_text(encoding="utf-8")
        for needed in (".env", "tasks/", "temp/"):
            self.assertIn(needed, text)


class TestPlatform(ApplyCase):
    def test_claude_by_default_has_no_codex_copies(self):
        self.install()
        self.assertFalse((self.target / "AGENTS.md").exists())
        self.assertFalse((self.target / ".agents").exists())
        self.assertTrue((self.target / ".claude" / "skills").is_dir())

    def test_codex_only(self):
        self.install("example.ru", "--platform", "codex")
        self.assertTrue((self.target / "AGENTS.md").is_file())
        self.assertTrue((self.target / ".agents" / "skills").is_dir())
        self.assertFalse((self.target / "CLAUDE.md").exists())
        self.assertFalse((self.target / ".claude").exists())

    def test_both(self):
        self.install("example.ru", "--platform", "both")
        for name in ("CLAUDE.md", "AGENTS.md"):
            self.assertTrue((self.target / name).is_file(), name)


class TestRepeatRun(ApplyCase):
    def test_second_run_changes_nothing(self):
        first = self.install()
        total = sum(first["data"]["summary"].values())
        payload = self.install()
        self.assertEqual(self.summary(payload).get("skip"), total, "всё должно быть пропущено")
        self.assertEqual(payload["data"]["written"], [])


class TestManualEditSurvives(ApplyCase):
    MARK = "\n## Правка человека\n"

    def test_edit_survives_and_new_version_goes_beside(self):
        self.install()
        target_file = self.target / "CLAUDE.md"
        edited = target_file.read_text(encoding="utf-8") + self.MARK
        target_file.write_text(edited, encoding="utf-8")

        payload = self.install(name="second.ru")

        self.assertIn(self.MARK.strip(), target_file.read_text(encoding="utf-8"))
        self.assertIn("example.ru", target_file.read_text(encoding="utf-8"))
        sidecar = self.target / "CLAUDE.md.new"
        self.assertTrue(sidecar.is_file(), "новая версия должна лечь рядом как .new")
        self.assertIn("second.ru", sidecar.read_text(encoding="utf-8"))
        self.assertGreaterEqual(self.summary(payload).get("sidecar", 0), 1)

    def test_owned_file_is_never_overwritten(self):
        """PRIORITIES.md человек ведёт сам — обновление каталога его не трогает."""
        self.install()
        priorities = self.target / "PRIORITIES.md"
        priorities.write_text("# Мой список\n- не трогать\n", encoding="utf-8")
        self.install(name="third.ru")
        self.assertEqual(priorities.read_text(encoding="utf-8"), "# Мой список\n- не трогать\n")

    def test_foreign_file_is_not_clobbered(self):
        """В непустой папке чужой файл с тем же именем остаётся нетронутым."""
        self.target.mkdir(parents=True)
        (self.target / "CLAUDE.md").write_text("мой старый файл\n", encoding="utf-8")
        self.install()
        self.assertEqual((self.target / "CLAUDE.md").read_text(encoding="utf-8"), "мой старый файл\n")
        self.assertTrue((self.target / "CLAUDE.md.new").is_file())


class TestGuards(ApplyCase):
    def test_refuses_target_inside_repo(self):
        code, payload = run_apply(
            "--target", str(ROOT / "inside"),
            "--preset", "minimal",
            "--set", "PROJECT_NAME=x",
            "--apply",
        )
        self.assertEqual(code, 1)
        self.assertIn("внутри репозитория", json.dumps(payload, ensure_ascii=False))

    def test_refuses_missing_placeholder(self):
        """Файл с незакрытым маркером не ставится — это не «мелочь на потом»."""
        code, payload = run_apply(
            "--target", str(self.target),
            "--preset", "minimal",
            "--apply",
        )
        self.assertEqual(code, 1)
        self.assertFalse(self.target.exists())
        names = [c["name"] for c in payload["checks"]]
        self.assertIn("placeholder_missing", names)

    def test_refuses_unknown_block(self):
        code, payload = run_apply(
            "--target", str(self.target),
            "--blocks", "core/нет-такого",
            "--apply",
        )
        self.assertEqual(code, 1)

    def test_refuses_empty_selection(self):
        code, _ = run_apply("--target", str(self.target), "--apply")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
