"""Документация для агента слушателя: практики на месте, ссылки не ведут в пустоту.

Агент, которому дали ссылку на репозиторий, ходит по относительным ссылкам из
README и AGENT-GUIDE. Битая ссылка для него — не мелкая опечатка, а тупик: он либо
остановится, либо достроит содержимое сам, что хуже.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv"}


def markdown_files() -> list:
    return [
        p for p in ROOT.rglob("*.md")
        if not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)
    ]


def block_dirs() -> list:
    return sorted(p.parent for p in (ROOT / "blocks").glob("*/*/block.json"))


class TestPractices(unittest.TestCase):
    def test_every_block_has_practice(self):
        for block in block_dirs():
            self.assertTrue((block / "README.md").is_file(), f"{block.name}: нет README.md с практикой")

    def test_guide_links_every_practice(self):
        guide = (ROOT / "AGENT-GUIDE.md").read_text(encoding="utf-8")
        for block in block_dirs():
            rel = block.relative_to(ROOT).as_posix() + "/README.md"
            self.assertIn(rel, guide, f"AGENT-GUIDE.md не ссылается на {rel}")

    def test_practice_has_recognition_signals(self):
        """Без сигналов распознавания агент не сможет сверить настройку с практикой."""
        for block in block_dirs():
            text = (block / "README.md").read_text(encoding="utf-8")
            self.assertIn("## Как понять, что это про вас", text, block.name)


class TestLinks(unittest.TestCase):
    def test_relative_links_resolve(self):
        broken = []
        for md in markdown_files():
            for target in LINK.findall(md.read_text(encoding="utf-8")):
                if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                    continue
                path = unquote(target.split("#", 1)[0])
                if not (md.parent / path).exists():
                    broken.append(f"{md.relative_to(ROOT).as_posix()} -> {target}")
        self.assertEqual(broken, [], "битые относительные ссылки")


if __name__ == "__main__":
    unittest.main()
