"""boot.fit, shared_parts, law_text and trace with a root: the pure parts the digest shares."""
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import boot, trace
from tests._hermetic import HermeticCase

NOWHERE = {"FRAMEWORK_ROOT": "/nonexistent/framework-root"}


class TestFit(HermeticCase):
    BUDGETS = {"a": (10, 50), "b": (10, 50), "c": (10, 50)}

    def test_each_layer_is_cut_to_its_maximum_marker_inside(self):
        out = boot.fit({"a": "x" * 200, "b": "y", "c": "z"}, self.BUDGETS, ["a", "b", "c"], 10_000)
        self.assertLessEqual(len(out["a"]), 50)
        self.assertIn("truncated, a", out["a"])
        self.assertEqual((out["b"], out["c"]), ("y", "z"))

    def test_the_total_governs_over_the_maxima(self):
        out = boot.fit({k: "w" * 50 for k in "abc"}, self.BUDGETS, ["a", "b", "c"], 100)
        self.assertLessEqual(sum(len(v) for v in out.values()), 100)
        self.assertLessEqual(len(out["a"]), 10)      # the first victim gave first

    def test_order_is_respected_and_the_loop_ends_at_the_minima(self):
        out = boot.fit({k: "w" * 50 for k in "abc"}, self.BUDGETS, ["c", "b"], 1)
        self.assertLessEqual(len(out["c"]), 10)
        self.assertLessEqual(len(out["b"]), 10)
        self.assertEqual(out["a"], "w" * 50)          # not a victim: never cut below its max

    def test_a_layer_absent_from_the_budgets_is_never_touched(self):
        law = "1. never truncated. " * 500
        out = boot.fit({"law": law, "a": "x" * 200}, self.BUDGETS, ["a"], 100)
        self.assertEqual(out["law"], law)

    def test_absent_victims_are_skipped_and_input_is_not_mutated(self):
        sections = {"a": "x" * 200}
        out = boot.fit(sections, self.BUDGETS, ["missing", "a"], 20)
        self.assertEqual(sections["a"], "x" * 200)
        self.assertLessEqual(len(out["a"]), 10)


class RootCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        shared = self.root / "shared"; shared.mkdir()
        (shared / "rule_quiet.md").write_text("---\nkind: rule\n---\nKeep replies short.\n")
        (shared / "ref_host.md").write_text("---\ndescription: the host map\n---\nbody\n")
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text("1. The law.\n")


class TestExplicitRoot(RootCase):
    def test_shared_parts_reads_the_given_root_not_the_environment(self):
        with mock.patch.dict(os.environ, NOWHERE):
            rules, index = boot.shared_parts(self.root)
        self.assertEqual(rules, ["### rule_quiet\nKeep replies short."])
        self.assertEqual(index, ["- `ref_host.md`: the host map"])

    def test_law_text_reads_the_given_root(self):
        with mock.patch.dict(os.environ, NOWHERE):
            self.assertEqual(boot.law_text(self.root), "1. The law.\n")
        self.assertEqual(boot.law_text(self.root / "nowhere"), "")

    def test_trace_summary_reads_the_given_roots_ledger(self):
        (self.root / "data").mkdir()
        con = sqlite3.connect(self.root / "data" / "trace-ledger.db")
        con.execute("CREATE TABLE trace_ledger (id INTEGER PRIMARY KEY, ts INTEGER, cousin TEXT,"
                    " tool TEXT, args_summary TEXT, result_summary TEXT)")
        con.execute("INSERT INTO trace_ledger (ts, cousin, tool, args_summary, result_summary)"
                    " VALUES (?, 'wren', 'cousin-memory', 'search x', '')", (int(time.time()),))
        con.commit(); con.close()
        with mock.patch.dict(os.environ, NOWHERE):
            self.assertIn("cousin-memory", trace.summary_for_boot("wren", root=self.root))


if __name__ == "__main__":
    unittest.main()
