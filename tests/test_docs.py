"""Small documentation check; standard library only, no installation needed."""

from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv"}


def markdown_files():
    return sorted(
        p for p in ROOT.rglob("*.md")
        if not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)
    )


def prose(path):
    # Example prompts and paths inside fenced code are not Markdown links.
    return re.sub(r"^```.*?^```[^\n]*", "", path.read_text(encoding="utf-8"),
                  flags=re.MULTILINE | re.DOTALL)


def anchors(path):
    seen = {}
    result = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", prose(path), re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


class TestDocumentation(unittest.TestCase):
    def test_gitignore_example_covers_both_layouts(self):
        """Data stays out of Git in both layouts; reusable files remain visible."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "--quiet", folder], check=True)
            (root / ".gitignore").write_bytes((ROOT / "examples/gitignore.txt").read_bytes())
            for prefix in ("", "sites/alfa/", "sites/beta/"):
                for name in ("tasks/a/report.md", "outputs/a/report.md", "inputs/source.csv",
                             "exports/data.csv", "temp/scratch.txt", ".env", ".env.local",
                             "secrets/account.json", "secrets/token.txt",
                             "report-service-account.json", "client-credentials.json",
                             "private.key", "private.pem"):
                    with self.subTest(path=prefix + name):
                        result = subprocess.run(
                            ["git", "-c", "core.excludesFile=", "check-ignore", "--no-index", "-q", prefix + name],
                            cwd=root,
                        )
                        self.assertEqual(result.returncode, 0)
            for name in ("PRIORITIES.md", "AGENTS.md", "TASKS.md", "skills/a.md", "tools/a.py",
                         "references/a.md", "DOSSIER.md", "ACCESS.md", ".env.example",
                         "config.json", "tests/fixtures/public-data.json",
                         "sites/alfa/config.json", "sites/alfa/.env.example",
                         "sites/alfa/DOSSIER.md", "sites/alfa/ACCESS.md", "sites/beta/DOSSIER.md"):
                with self.subTest(path=name):
                    result = subprocess.run(
                        ["git", "-c", "core.excludesFile=", "check-ignore", "--no-index", "-q", name],
                        cwd=root,
                    )
                    self.assertEqual(result.returncode, 1)

    def test_entry_points_link_all_practices_and_examples(self):
        practices = sorted((ROOT / "practices").glob("*.md"))
        examples = sorted(p for p in (ROOT / "examples").iterdir() if p.is_file())
        self.assertEqual(len(practices), 6)
        self.assertEqual(len(examples), 6)
        for entry in ("README.md", "AGENT-GUIDE.md"):
            links = LINK.findall(prose(ROOT / entry))
            for path in practices + examples:
                with self.subTest(entry=entry, path=path.name):
                    self.assertIn(path.relative_to(ROOT).as_posix(), links)

    def test_internal_links_resolve(self):
        for md in markdown_files():
            for target in LINK.findall(prose(md)):
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue
                path = (md.parent / unquote(parsed.path)).resolve() if parsed.path else md
                with self.subTest(document=md.relative_to(ROOT), target=target):
                    self.assertTrue(path.is_relative_to(ROOT), "Link leaves repository")
                    self.assertTrue(path.exists(), "Missing internal target")
                    if parsed.fragment and path.suffix == ".md":
                        self.assertIn(unquote(parsed.fragment), anchors(path), "Missing heading")


if __name__ == "__main__":
    unittest.main()
