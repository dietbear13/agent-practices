"""Каркас скрипта: стоп-кран, дедупликация, ретраи, метаданные качества.

Код перенесён из первой версии и там был покрыт тестами на pytest. Здесь он
переписан на unittest, потому что весь репозиторий обязан проверяться голым
`python run_tests.py` без установки чего-либо.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "blocks/core/script-runtime/tools"))

import runtime as r  # noqa: E402

CLEANUP = ROOT / "blocks/core/task-folder/tools/cleanup_task.py"
NEW_TASK = ROOT / "blocks/core/task-folder/tools/new_task.py"


class TestBudget(unittest.TestCase):
    def test_steps_limit(self):
        with self.assertRaises(r.BudgetExceeded):
            r.Budget(max_steps=3).check(steps_done=3, seconds_elapsed=0)

    def test_seconds_limit(self):
        with self.assertRaises(r.BudgetExceeded):
            r.Budget(max_seconds=60).check(steps_done=0, seconds_elapsed=60)

    def test_cost_limit(self):
        with self.assertRaises(r.BudgetExceeded):
            r.Budget(max_cost=5.0).check(steps_done=0, seconds_elapsed=0, cost_so_far=5.0)

    def test_none_means_no_limit(self):
        r.Budget().check(steps_done=10**6, seconds_elapsed=10**6, cost_so_far=10**6)

    def test_message_is_readable(self):
        try:
            r.Budget(max_steps=2).check(steps_done=2, seconds_elapsed=0)
        except r.BudgetExceeded as exc:
            self.assertIn("лимит шагов", str(exc))


class TestRetryClassification(unittest.TestCase):
    def test_429_without_retry_after_is_not_retried(self):
        """Гадать интервал у сервиса с квотой нельзя: так её выжигают за минуту."""
        self.assertEqual(r.classify_error(429, None, False), r.RetryDecision.NO_RETRY)

    def test_429_with_retry_after(self):
        self.assertEqual(r.classify_error(429, 5.0, False), r.RetryDecision.RETRY_AFTER)

    def test_5xx_and_timeout_backoff(self):
        self.assertEqual(r.classify_error(503, None, False), r.RetryDecision.BACKOFF)
        self.assertEqual(r.classify_error(None, None, True), r.RetryDecision.BACKOFF)
        self.assertEqual(r.classify_error(408, None, False), r.RetryDecision.BACKOFF)

    def test_4xx_is_final(self):
        for code in (400, 401, 403, 404):
            self.assertEqual(r.classify_error(code, None, False), r.RetryDecision.NO_RETRY)

    def test_backoff_grows_and_is_capped(self):
        self.assertLessEqual(r.backoff_delay(0, base=1.0, cap=30.0), 1.0)
        self.assertLessEqual(r.backoff_delay(10, base=1.0, cap=30.0), 30.0)


class TestCheckpoint(unittest.TestCase):
    def test_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".checkpoint.json"
            cp = r.Checkpoint()
            cp.mark_done("/a")
            cp.save(path)
            self.assertTrue(r.Checkpoint.load(path).is_done("/a"))
            self.assertFalse(r.Checkpoint.load(path).is_done("/b"))

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(r.Checkpoint.load(Path(tmp) / "нет.json").done_ids, set())


class TestQualityMetadata(unittest.TestCase):
    def base(self, **kw):
        params = dict(
            rows=10, requests_count=1, retries=0,
            period_start="2026-09-01", period_end="2026-09-30",
        )
        params.update(kw)
        return r.build_quality_metadata(**params)

    def test_zero_rows_is_degraded(self):
        self.assertTrue(self.base(rows=0).degraded)

    def test_rows_equal_page_limit_is_degraded(self):
        """Совпадение с лимитом страницы почти всегда означает обрезанную пагинацию."""
        meta = self.base(rows=1000, page_limit=1000)
        self.assertTrue(meta.degraded)
        self.assertIn("пагинац", meta.degraded_reason)

    def test_sampling_is_degraded(self):
        self.assertTrue(self.base(sampled=True, sample_share=0.1).degraded)

    def test_normal_is_not_degraded(self):
        meta = self.base(rows=42, page_limit=1000)
        self.assertFalse(meta.degraded)
        self.assertIsNone(meta.degraded_reason)


class TestResultRecording(unittest.TestCase):
    def test_writing_also_registers_in_manifest(self):
        """Запись и регистрация неразделимы: иначе чистильщик снесёт забытый результат."""
        with tempfile.TemporaryDirectory() as tmp:
            out = r.output_dir(Path(tmp), "demo")
            meta = r.build_quality_metadata(
                rows=1, requests_count=1, retries=0,
                period_start="2026-09-01", period_end="2026-09-30",
            )
            r.record_result(out, "report.csv", [{"url": "/a"}], ["url"], meta)
            entries = r.manifest_entries(out)
            self.assertEqual([e["path"] for e in entries], ["report.csv"])
            self.assertTrue((out / "report.csv.quality.json").is_file())

    def test_csv_has_bom_for_excel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.csv"
            r.write_csv(path, [{"название": "значение"}], ["название"])
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))


class TestSecrets(unittest.TestCase):
    def test_reads_key_and_strips_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_bytes("﻿TOKEN=secret-value\n# коммент\n".encode("utf-8"))
            self.assertEqual(r.read_secret("TOKEN", env_path=env), "secret-value")

    def test_missing_key_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                r.read_secret("TOKEN", env_path=env)
            self.assertIn(".env", str(ctx.exception))

    def test_missing_file_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                r.read_secret("TOKEN", env_path=Path(tmp) / ".env")
            self.assertIn(".env.example", str(ctx.exception))


class TestTaskFolder(unittest.TestCase):
    def make(self, tmp: str, manifest) -> Path:
        folder = Path(tmp) / "2026-09-05-audit"
        folder.mkdir()
        (folder / "report.md").write_text("итог", encoding="utf-8")
        (folder / "report.md.quality.json").write_text("{}", encoding="utf-8")
        (folder / "черновик.tmp").write_text("мусор", encoding="utf-8")
        (folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return folder

    def run_cleanup(self, folder: Path, *args: str):
        return subprocess.run(
            [sys.executable, str(CLEANUP), str(folder), *args],
            capture_output=True, text=True, encoding="utf-8",
        )

    def test_dry_run_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make(tmp, {"keep": ["report.md"]})
            self.run_cleanup(folder)
            self.assertTrue((folder / "черновик.tmp").exists())

    def test_keep_form_from_the_talk(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make(tmp, {"keep": ["report.md"]})
            self.run_cleanup(folder, "--apply")
            self.assertTrue((folder / "report.md").exists())
            self.assertFalse((folder / "черновик.tmp").exists())

    def test_list_form_from_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make(tmp, [{"path": "report.md"}])
            self.run_cleanup(folder, "--apply")
            self.assertTrue((folder / "report.md").exists())
            self.assertFalse((folder / "черновик.tmp").exists())

    def test_quality_metadata_is_protected(self):
        """Снести метаданные — значит уничтожить проверяемость выгрузки."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make(tmp, {"keep": ["report.md"]})
            self.run_cleanup(folder, "--apply")
            self.assertTrue((folder / "report.md.quality.json").exists())

    def test_without_manifest_refuses_to_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-09-05-audit"
            folder.mkdir()
            (folder / "что-то.tmp").write_text("x", encoding="utf-8")
            proc = self.run_cleanup(folder, "--apply")
            self.assertEqual(proc.returncode, 1)
            self.assertTrue((folder / "что-то.tmp").exists())

    def test_new_task_transliterates_russian_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(NEW_TASK), "аудит микроразметки",
                 "--date", "2026-09-05", "--root", str(Path(tmp) / "tasks")],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            folder = Path(tmp) / "tasks" / "2026-09-05-audit-mikrorazmetki"
            self.assertTrue(folder.is_dir())
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest, {"keep": []})

    def test_new_task_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = [sys.executable, str(NEW_TASK), "audit", "--date", "2026-09-05",
                    "--root", str(Path(tmp) / "tasks")]
            subprocess.run(args, capture_output=True, text=True)
            proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
