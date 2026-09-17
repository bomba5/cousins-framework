"""Boot packet assembly: budgets, degraded detection, composition.

The three incident-lessons are pinned as tests: the total ceiling
governs over per-layer maxima, truncation markers count inside their
budget, and degraded detection is per-layer logic that never flags a
healthy cousin.
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.boot import (
    TOTAL_MAX_CHARS,
    _truncate,
    assemble,
    bump_generation,
    read_generation,
)


class BootCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _healthy_home(self):
        (self.home / "STATUS.md").write_text(
            "# Wren - STATUS\n\n## Open loops\n\n- finish the report\n"
        )
        (self.home / "data" / "handoff.md").write_text("# Handoff\nmid-report\n")
        (self.home / "data" / "active-threads.md").write_text(
            "# Threads\n- report: drafting\n"
        )
        (self.home / "self-portrait.md").write_text(
            "# Cousin Self-Portrait: wren\n## Voice\nPlain.\n"
            "## Operator Calibration\nShort statuses.\n"
        )
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "law.md").write_text(
            "1. Persona is authored, never improvised.\n"
        )
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "tool-surface.md").write_text(
            "# Tool Surface\n\n- `cousin-memory` - usage: cousin-memory\n"
        )


class TestGeneration(BootCase):
    def test_counter_initializes_at_zero_and_bumps(self):
        self.assertEqual(read_generation(self.home), 0)
        self.assertEqual(bump_generation(self.home), 1)
        self.assertEqual(read_generation(self.home), 1)


class TestTruncate(BootCase):
    def test_marker_counts_inside_the_budget(self):
        # A marker appended beyond the slice re-triggers the overflow
        # loop on the same victim forever - the flip-hang incident.
        out = _truncate("x" * 500, 100, "layer")
        self.assertLessEqual(len(out), 100)
        self.assertIn("truncated", out)


class TestAssemble(BootCase):
    def test_healthy_home_composes_all_sections_not_degraded(self):
        self._healthy_home()
        pkt = assemble("wren", self.home, generation=3)
        self.assertEqual(pkt["degraded_sections"], [])
        for header in ("Framework Law", "Self-Portrait",
                       "Operator Calibration", "Active State",
                       "Task Packet", "Tool Trace", "Retrieved Memories",
                       "Required Boot Actions"):
            self.assertIn(header, pkt["text"])
        self.assertIn("Generation: 3", pkt["text"])
        self.assertIn("identity_hash", pkt["text"])

    def test_fresh_home_names_its_real_gaps_and_only_those(self):
        # Empty memories and empty trace are the starting condition,
        # never degraded; a missing portrait and missing state are.
        pkt = assemble("wren", self.home, generation=1)
        self.assertIn("self_portrait", pkt["degraded_sections"])
        self.assertIn("active_state", pkt["degraded_sections"])
        self.assertNotIn("memories", pkt["degraded_sections"])
        self.assertNotIn("trace_summary", pkt["degraded_sections"])

    def test_task_fallback_with_real_active_state_is_not_degraded(self):
        self._healthy_home()
        (self.home / "data" / "active-threads.md").unlink()
        pkt = assemble("wren", self.home, generation=1)
        self.assertNotIn("task_packet", pkt["degraded_sections"])

    def test_total_ceiling_governs_over_per_layer_maxima(self):
        self._healthy_home()
        # Bloat several layers to their individual maxima.
        (self.home / "self-portrait.md").write_text(
            "# P\n## Voice\n" + "portrait words " * 3000)
        (self.home / "data" / "handoff.md").write_text("h " * 20000)
        (self.home / "data" / "active-threads.md").write_text("t " * 20000)
        raw = self.home / "memory" / "raw"
        raw.mkdir(exist_ok=True)
        with open(raw / "2026-08-06.jsonl", "w") as fh:
            for i in range(400):
                fh.write(json.dumps({"topic": "t%d" % i,
                                     "content": "m" * 300}) + "\n")
        pkt = assemble("wren", self.home, generation=1)
        self.assertLessEqual(pkt["chars"], TOTAL_MAX_CHARS)

    def test_staleness_warning_when_decisions_outrun_status(self):
        self._healthy_home()
        old = time.time() - 3600
        os.utime(self.home / "STATUS.md", (old, old))
        with open(self.home / "data" / "decisions.jsonl", "w") as fh:
            fh.write(json.dumps({
                "timestamp": "2099-01-01T00:00:00+00:00",
                "topic": "late", "decision": "d", "reasoning": "r",
            }) + "\n")
        pkt = assemble("wren", self.home, generation=1)
        self.assertIn("STALE WARNING", pkt["text"])


if __name__ == "__main__":
    unittest.main()
