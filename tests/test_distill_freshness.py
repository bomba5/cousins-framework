"""Keeping the distilled floor level with raw.

The views were rebuilt only at a start or a flip, and a view is
rewritten only when its text changes, so the console's memory view
said "raw has entries newer than the distilled views" almost always -
including right after a distill that had nothing to change. distill
now stamps every run; the explorer compares raw against the stamp,
and the loops daemon distills any cousin whose raw is newer.
"""
import os
import pathlib
import tempfile
import time
import unittest

from cousin_lib import distill, memory
from cousin_lib import memory_explorer as mx
from tests.test_loops import LoopsCase


def _plant(home, topic, content):
    memory._append_raw(home, {"topic": topic, "content": content,
                              "truth_level": "cousin-conclusion",
                              "source": "decision"})


class HomeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)


class TestRunStamp(HomeCase):
    def test_every_run_stamps_even_when_no_view_changes(self):
        _plant(self.home, "retention", "keep thirty days")
        distill.distill(self.home)
        first = distill.last_run(self.home)
        os.utime(distill.last_run_path(self.home), (first - 60, first - 60))
        views = {p: p.stat().st_mtime for p in
                 memory.distilled_dir(self.home).glob("*.md")}
        distill.distill(self.home)
        self.assertGreater(distill.last_run(self.home), first - 60)
        # nothing to change, so no view was rewritten
        self.assertEqual(views, {p: p.stat().st_mtime for p in
                                 memory.distilled_dir(self.home).glob("*.md")})

    def test_no_raw_means_nothing_to_do(self):
        self.assertFalse(distill.distill_if_behind(self.home))
        self.assertIsNone(distill.last_run(self.home))

    def test_distills_only_when_raw_is_newer_than_the_last_run(self):
        _plant(self.home, "retention", "keep thirty days")
        self.assertTrue(distill.distill_if_behind(self.home))
        self.assertFalse(distill.distill_if_behind(self.home))
        stamp = distill.last_run(self.home)
        os.utime(distill.last_run_path(self.home), (stamp - 60, stamp - 60))
        _plant(self.home, "retention", "keep sixty days")
        self.assertTrue(distill.distill_if_behind(self.home))
        text = (memory.distilled_dir(self.home) / "decisions.md").read_text()
        self.assertIn("keep sixty days", text)


class TestExplorerNote(HomeCase):
    def test_a_distill_that_changed_nothing_still_clears_behind(self):
        _plant(self.home, "retention", "keep thirty days")
        distill.distill(self.home)
        # Age the views and the stamp, then a raw write that changes
        # no view line (same topic, same content).
        old = time.time() - 3600
        for p in memory.distilled_dir(self.home).iterdir():
            os.utime(p, (old, old))
        _plant(self.home, "retention", "keep thirty days")
        self.assertTrue(mx.overview(self.home)["insights"]
                        ["distilled_behind_raw"])
        distill.distill(self.home)
        self.assertFalse(mx.overview(self.home)["insights"]
                         ["distilled_behind_raw"])


class TestLoopsKeepsTheFloor(LoopsCase):
    def test_tick_distills_a_cousin_whose_raw_is_newer(self):
        home = self._cousin("wren")
        _plant(home, "retention", "keep thirty days")
        report = self._tick()
        self.assertEqual(report["distilled"], ["wren"])
        self.assertIn("keep thirty days",
                      (memory.distilled_dir(home) / "decisions.md")
                      .read_text())
        self.assertEqual(self._tick()["distilled"], [])

    def test_a_stopped_cousin_is_kept_level_too(self):
        home = self._cousin("wren")
        _plant(home, "retention", "keep thirty days")
        report = self._tick(is_alive=lambda slug: False)
        self.assertEqual(report["distilled"], ["wren"])


if __name__ == "__main__":
    unittest.main()
