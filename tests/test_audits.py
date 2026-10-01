"""Expected-action audits: session-end checks and the baseline fallback.

Real files, real mtimes, real SQLite. The one subtlety worth naming:
an audit may only demand actions that shipped tools actually produce -
demanding an artifact with no producer files a violation on every
session for every cousin, and that failure is why the durable-memory
check spans every surface a cousin writes.
"""
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.audits import (
    audit_before_exit,
    derive_active_threads_baseline,
    list_violations,
    mark_corrected,
    write_active_threads_baseline,
)


class AuditsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _touch_fresh(self, relpath):
        path = self.home / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content\n")
        return path


class TestBeforeExit(AuditsCase):
    def test_all_surfaces_fresh_means_no_violations(self):
        since = time.time() - 60
        for rel in ("STATUS.md", "data/handoff.md",
                    "data/active-threads.md", "memory/note.md"):
            self._touch_fresh(rel)
        violations = audit_before_exit("wren", str(self.home),
                                       since_ts=since)
        self.assertEqual(violations, [])

    def test_stale_surfaces_are_reported_and_recorded(self):
        for rel in ("STATUS.md", "data/handoff.md",
                    "data/active-threads.md", "memory/note.md"):
            self._touch_fresh(rel)
        since = time.time() + 60  # session "started" after every write
        violations = audit_before_exit("wren", str(self.home),
                                       since_ts=since)
        actions = {v["action"] for v in violations}
        self.assertEqual(actions, {
            "update_status", "write_handoff", "mark_open_threads",
            "extract_durable_memories",
        })
        recorded = list_violations("wren")
        self.assertEqual(len(recorded), 4)

    def test_any_durable_memory_surface_counts(self):
        # The producer-less-audit lesson: raw candidates, the decisions
        # log, and memory/*.md are all legitimate proof of extraction;
        # demanding one specific file fires noise on every session.
        since = time.time() - 60
        for rel in ("STATUS.md", "data/handoff.md",
                    "data/active-threads.md"):
            self._touch_fresh(rel)
        self._touch_fresh("data/decisions.jsonl")
        violations = audit_before_exit("wren", str(self.home),
                                       since_ts=since)
        self.assertEqual(violations, [])

    def test_index_artifacts_do_not_count_as_memory(self):
        since = time.time() - 60
        for rel in ("STATUS.md", "data/handoff.md",
                    "data/active-threads.md"):
            self._touch_fresh(rel)
        self._touch_fresh("memory/embeddings.json")  # reindex churn
        violations = audit_before_exit("wren", str(self.home),
                                       since_ts=since)
        self.assertEqual([v["action"] for v in violations],
                         ["extract_durable_memories"])

    def test_mark_corrected_stamps_the_row(self):
        self._touch_fresh("STATUS.md")
        audit_before_exit("wren", str(self.home),
                          since_ts=time.time() + 60)
        row = list_violations("wren")[0]
        self.assertIsNone(row["corrected_at"])
        mark_corrected(row["id"])
        row = list_violations("wren")[0]
        self.assertIsNotNone(row["corrected_at"])


class TestActiveThreadsBaseline(AuditsCase):
    _STATUS = (
        "# Wren - STATUS\n\n"
        "## Open loops\n\n- finish the report\n- chase the reviewer\n\n"
        "## Done\n\n- shipped a thing\n\n"
        "## Open loops (next week)\n\n- plan the migration\n"
    )

    def test_derives_the_live_open_loops_section_only(self):
        # the bare heading is the live section; a suffixed one is history
        (self.home / "STATUS.md").write_text(self._STATUS)
        body = derive_active_threads_baseline(str(self.home))
        self.assertIn("finish the report", body)
        self.assertNotIn("plan the migration", body)
        self.assertNotIn("shipped a thing", body)
        self.assertIn("baseline derived", body)  # the tell-apart marker

    def test_writes_when_missing_and_skips_when_fresh(self):
        (self.home / "STATUS.md").write_text(self._STATUS)
        out = write_active_threads_baseline(str(self.home), since_ts=0.0)
        self.assertEqual(out["reason"], "wrote-new")
        # The cousin then writes real content this session.
        session_start = time.time() - 30
        out = write_active_threads_baseline(str(self.home),
                                            since_ts=session_start)
        self.assertEqual((out["wrote"], out["reason"]),
                         (False, "fresh-skip"))

    def test_no_open_loops_writes_nothing(self):
        (self.home / "STATUS.md").write_text("# Wren - STATUS\n")
        out = write_active_threads_baseline(str(self.home), since_ts=0.0)
        self.assertEqual((out["wrote"], out["reason"]),
                         (False, "no-open-loops"))


if __name__ == "__main__":
    unittest.main()
