"""Примитивы из bootstrap/_common.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bootstrap"))

import _common as c  # noqa: E402


class TestHash(unittest.TestCase):
    def test_lf_and_crlf_give_same_hash(self):
        """Главный кросс-платформенный инвариант.

        git с core.autocrlf=true перепишет переводы строк при checkout на
        Windows. Если хеш это учитывает, система решит, что человек правил
        файл, которого он не открывал.
        """
        self.assertEqual(c.sha256_bytes(b"a\nb\n"), c.sha256_bytes(b"a\r\nb\r\n"))
        self.assertEqual(c.sha256_bytes(b"a\nb\n"), c.sha256_bytes(b"a\rb\r"))

    def test_binary_is_hashed_as_is(self):
        blob = b"\x00\x01\r\n\x02"
        self.assertTrue(c.looks_binary(blob))
        self.assertNotEqual(c.sha256_bytes(blob), c.sha256_bytes(blob.replace(b"\r\n", b"\n")))

    def test_cyrillic_text(self):
        self.assertEqual(c.sha256_text("Метрика"), c.sha256_bytes("Метрика".encode("utf-8")))


class TestPlaceholders(unittest.TestCase):
    def test_find(self):
        found = c.find_placeholders("сайт <<<SITE>>>, счётчик <<<COUNTER>>>")
        self.assertEqual(found, {"SITE", "COUNTER"})

    def test_substitute_reports_missing(self):
        text, missing = c.substitute("<<<A>>> и <<<B>>>", {"A": "раз"})
        self.assertEqual(text, "раз и <<<B>>>")
        self.assertEqual(missing, {"B"})

    def test_missing_is_not_replaced_by_empty(self):
        """Незакрытый маркер должен остаться видимым, а не превратиться в пустоту."""
        text, _ = c.substitute("значение: <<<X>>>", {})
        self.assertIn("<<<X>>>", text)

    def test_multiline_is_not_a_placeholder(self):
        self.assertEqual(c.find_placeholders("<<<не\nимя>>>"), set())


class TestAtomicWrite(unittest.TestCase):
    def test_writes_utf8_and_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "файл.md"
            c.write_text_atomic(path, "строка\nвторая\n")
            self.assertEqual(path.read_bytes(), "строка\nвторая\n".encode("utf-8"))

    def test_no_temp_files_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            c.write_text_atomic(path, "x")
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.startswith(".tmp-")]
            self.assertEqual(leftovers, [])

    def test_env_read_strips_bom(self):
        """.env, сохранённый Блокнотом, не должен ломать первую переменную."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_bytes("﻿TOKEN=abc\n".encode("utf-8"))
            self.assertTrue(c.read_text_env(path).startswith("TOKEN="))


class TestResult(unittest.TestCase):
    def test_failure_sets_exit_code(self):
        r = c.Result(action="t")
        r.ok("a", "всё хорошо")
        self.assertEqual(r.exit_code(), c.EXIT_OK)
        r.fail("b", "плохо")
        self.assertEqual(r.exit_code(), c.EXIT_USER)

    def test_warn_is_not_a_failure(self):
        r = c.Result(action="t")
        r.warn("a", "внимание")
        self.assertEqual(r.exit_code(), c.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
