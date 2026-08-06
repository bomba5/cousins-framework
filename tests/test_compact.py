"""Memory-index compaction: pointer hygiene under hard invariants.

The index (MEMORY.md) is one pointer line per topic file, loaded every
session - compacting it is hygiene, not deletion: a pointer may be
retired ONLY when its topic file is independently reachable (on disk
AND in the search index), so nothing is ever lost. Uncertainty keeps:
an unparsable date is a keep, not a candidate.
"""
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.compact import compact_index
from cousin_lib.memory_search import build_index


class CompactCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _index_line(self, name, title, date="2026-01-01", pin=False):
        return "- [%s](%s) - notes %s%s\n" % (
            title, name, date, " [pin]" if pin else "")

    def _topic(self, name, body="the fact\n"):
        (self.home / "memory" / name).write_text(body)


class TestRetireRules(CompactCase):
    def test_old_reachable_pointer_is_retired_file_stays(self):
        self._topic("project_old.md")
        self._topic("project_new.md")
        (self.home / "MEMORY.md").write_text(
            "# Wren - memory index\n"
            + self._index_line("project_old.md", "Old thing")
            + self._index_line("project_new.md", "New thing",
                               date=time.strftime("%Y-%m-%d")))
        build_index(self.home)
        report = compact_index(self.home, budget=120, hot_days=7)
        self.assertTrue(report["ok"])
        index = (self.home / "MEMORY.md").read_text()
        self.assertNotIn("project_old.md", index)
        self.assertIn("project_new.md", index)
        # Retired from the index, never from disk.
        self.assertTrue((self.home / "memory" / "project_old.md").exists())

    def test_unreachable_pointer_is_kept_whatever_its_age(self):
        # The topic file is missing from disk: retiring the pointer
        # would orphan the knowledge entirely. Keep.
        (self.home / "MEMORY.md").write_text(
            "# Wren - memory index\n"
            + self._index_line("project_gone.md", "Gone thing"))
        build_index(self.home)
        report = compact_index(self.home, budget=10, hot_days=7)
        self.assertIn("project_gone.md",
                      (self.home / "MEMORY.md").read_text())

    def test_unparsable_date_keeps_uncertainty_is_a_keep(self):
        self._topic("project_undated.md")
        (self.home / "MEMORY.md").write_text(
            "# Wren - memory index\n"
            "- [Undated](project_undated.md) - no date here\n")
        build_index(self.home)
        compact_index(self.home, budget=10, hot_days=7)
        self.assertIn("project_undated.md",
                      (self.home / "MEMORY.md").read_text())

    def test_pinned_and_hot_pointers_are_kept(self):
        self._topic("project_pinned.md")
        self._topic("project_hot.md")
        (self.home / "MEMORY.md").write_text(
            "# Wren - memory index\n"
            + self._index_line("project_pinned.md", "Pinned",
                               date="2020-01-01", pin=True)
            + self._index_line("project_hot.md", "Hot",
                               date=time.strftime("%Y-%m-%d")))
        build_index(self.home)
        compact_index(self.home, budget=10, hot_days=7)
        index = (self.home / "MEMORY.md").read_text()
        self.assertIn("project_pinned.md", index)
        self.assertIn("project_hot.md", index)

    def test_stops_at_budget_oldest_first(self):
        for i in range(4):
            self._topic("project_%d.md" % i)
        lines = "".join(
            self._index_line("project_%d.md" % i, "T%d" % i,
                             date="2026-01-0%d" % (i + 1))
            for i in range(4))
        (self.home / "MEMORY.md").write_text(
            "# Wren - memory index\n" + lines)
        build_index(self.home)
        # Budget already satisfied after retiring the two oldest.
        target = len("# Wren - memory index\n") + 2 * len(
            self._index_line("project_0.md", "T0"))
        compact_index(self.home, budget=target + 20, hot_days=0)
        index = (self.home / "MEMORY.md").read_text()
        self.assertNotIn("project_0.md", index)
        self.assertIn("project_3.md", index)


class TestSafetyNet(CompactCase):
    def test_a_snapshot_precedes_every_run(self):
        self._topic("project_x.md")
        (self.home / "MEMORY.md").write_text(
            "# Wren - memory index\n"
            + self._index_line("project_x.md", "X"))
        build_index(self.home)
        compact_index(self.home, budget=10, hot_days=7)
        snapshots = list(
            (self.home / "memory" / ".compact-snapshots").glob("*"))
        self.assertEqual(len(snapshots), 1)

    def test_dry_run_changes_nothing(self):
        self._topic("project_old.md")
        body = ("# Wren - memory index\n"
                + self._index_line("project_old.md", "Old"))
        (self.home / "MEMORY.md").write_text(body)
        build_index(self.home)
        report = compact_index(self.home, budget=10, hot_days=7,
                               dry_run=True)
        self.assertEqual((self.home / "MEMORY.md").read_text(), body)
        self.assertTrue(report["would_retire"])


if __name__ == "__main__":
    unittest.main()
