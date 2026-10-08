"""docs/reference/state.md names every store (#285): a `.db`, `.jsonl`
or `.json` name in cousin_lib that the page does not mention, and that
NOT_STORES does not list as config or another program's file, fails
here, so a new store gets its row (level, writer, what wins)."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "reference" / "state.md"
NAME = re.compile(r"""["']([A-Za-z0-9_.-]+\.(?:db|jsonl|json))["']""")
# names that are no store of this install: an import source read once,
# another program's files, config, manifests inside an export or backup
NOT_STORES = {
    "memory.jsonl", "-digest.jsonl",
    ".claude.json", ".credentials.json", "auth.json", "settings.json", "settings.local.json",
    "package-lock.json", "manifest.json", "MANIFEST.json", ".manifest.json", ".meta.json",
    ".baseline.json", "embeddings.json", ".mcp.json", "chat-hooks.json",
    "net-allowlist.json", "outbound-filter.json", "shared-reviewers.json",
    "console-users.json", "cousin-policy.json", "cousin-policy.ack.json",
}


def store_names():
    names = set()
    for path in (ROOT / "cousin_lib").rglob("*.py"):
        names.update(NAME.findall(path.read_text(encoding="utf-8")))
    return names - NOT_STORES


class TestStateDoc(unittest.TestCase):
    def test_every_store_name_has_a_row(self):
        doc = DOC.read_text(encoding="utf-8")
        missing = sorted(n for n in store_names() if n not in doc)
        self.assertEqual(missing, [], "add a row to docs/reference/state.md")

    def test_the_scan_finds_the_stores_it_should(self):
        names = store_names()
        for expected in ("inbox.db", "chat.db", "jobs.db", "decisions.jsonl"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
