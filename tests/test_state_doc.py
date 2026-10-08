"""docs/reference/state.md names every store (#285): a `.db` or `.jsonl`
name in cousin_lib that the page does not mention fails here, so a new
store gets its row (level, writer, what wins) when it is added."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "reference" / "state.md"
NAME = re.compile(r"""["']([A-Za-z0-9_.-]+\.(?:db|jsonl))["']""")
# names that are no store of this install: an import source read once
NOT_STORES = {"memory.jsonl", "-digest.jsonl"}


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
