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

from cousin_lib import trace
from cousin_lib.boot import (
    TOTAL_MAX_CHARS,
    _truncate,
    assemble,
    bump_generation,
    generation_started,
    mark_generation_start,
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


class TestGenerationStart(BootCase):
    """When the current generation's session started: what the daily flip
    reads to leave a session younger than the day's flip point alone."""

    def stamp(self):
        return json.loads((self.home / "data" / "generation-started.json").read_text())

    def test_a_home_that_never_started_a_generation_has_no_start(self):
        self.assertIsNone(generation_started(self.home))

    def test_a_bump_records_the_new_generations_start(self):
        before = time.time()
        bump_generation(self.home)
        self.assertEqual(self.stamp()["generation"], 1)
        self.assertGreaterEqual(generation_started(self.home), before)
        self.assertLessEqual(generation_started(self.home), time.time())

    def test_a_start_without_a_bump_is_recorded_too(self):
        # a runner's first boot: a session starts, the generation stays 0
        mark_generation_start(self.home, now=1000.0)
        self.assertEqual(self.stamp(), {"generation": 0, "started": 1000.0})
        self.assertEqual(generation_started(self.home), 1000.0)

    def test_a_home_from_before_the_stamp_falls_back_to_its_last_bump(self):
        path = self.home / "data" / "generation.txt"
        path.write_text("4")
        os.utime(path, (2000.0, 2000.0))
        self.assertEqual(generation_started(self.home), 2000.0)

    def test_a_session_on_file_with_no_generation_record_started_at_an_unknown_time(self):
        # a home from before the stamp that never rolled over: started,
        # when unknown, so as old as can be (the daily flip ends it)
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-1", "updated": 9e9}))
        self.assertEqual(generation_started(self.home), 0.0)

    def test_a_torn_stamp_falls_back(self):
        (self.home / "data" / "generation-started.json").write_text("{")
        self.assertIsNone(generation_started(self.home))


class TestTruncate(BootCase):
    def test_marker_counts_inside_the_budget(self):
        # enforces: law 14
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
        # enforces: law 14
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


class TestTheLawIsAHardLayer(BootCase):
    """The law reaches a packet whole or the packet says so. It used to be
    cut to 3200 chars by the per-layer cap that runs above the overflow
    loop, so a packet could carry rules 1-8 and drop 9-14 - including the
    two that keep one cousin's memory out of another's."""

    def _law(self, text):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "law.md").write_text(text)

    def test_the_whole_law_reaches_the_packet(self):
        self._healthy_home()
        law = "".join("%d. rule %d that a cousin must read.\n" % (i, i)
                      for i in range(1, 60))
        self._law(law)
        pkt = assemble("wren", self.home, generation=1)
        self.assertIn("14. rule 14 that a cousin must read.", pkt["text"])
        self.assertNotIn("truncated, law", pkt["text"])
        self.assertEqual(pkt["incomplete_layers"], [])
        self.assertNotIn("law", pkt["degraded_sections"])

    def test_a_law_too_long_for_the_total_is_reported_not_trimmed(self):
        # enforces: law 14
        self._healthy_home()
        law = "rule. " * 9000
        self._law(law)
        pkt = assemble("wren", self.home, generation=1)
        self.assertIn("LAW INCOMPLETE", pkt["text"])
        self.assertIn("read the whole of it at", pkt["text"])
        self.assertIn(law.strip(), pkt["text"])
        self.assertNotIn("truncated, law", pkt["text"])
        self.assertEqual(pkt["incomplete_layers"], ["law"])

    def test_the_overflow_is_in_the_trace_ledger_not_only_the_packet(self):
        self._healthy_home()
        self._law("rule. " * 9000)
        assemble("wren", self.home, generation=1)
        summary = trace.summary_for_boot("wren", root=self.root)
        self.assertIn("LAW INCOMPLETE", summary)

    def test_a_home_with_no_law_file_boots_degraded_and_says_so(self):
        # An install whose seed never ran is the worst boot there is; it
        # was filed as somebody else's problem and reported nowhere.
        self._healthy_home()
        (self.root / "config" / "law.md").unlink()
        pkt = assemble("wren", self.home, generation=1)
        self.assertIn("law", pkt["degraded_sections"])
        self.assertIn("DEGRADED layers: law", pkt["text"])

    def test_the_law_is_still_inside_the_total_when_the_total_allows(self):
        self._healthy_home()
        (self.home / "self-portrait.md").write_text(
            "# P\n## Voice\n" + "portrait words " * 3000)
        (self.home / "data" / "handoff.md").write_text("h " * 20000)
        pkt = assemble("wren", self.home, generation=1)
        self.assertLessEqual(pkt["chars"], TOTAL_MAX_CHARS)
        self.assertIn("Framework Law", pkt["text"])


if __name__ == "__main__":
    unittest.main()


class TestSharedLayer(BootCase):
    """Operator rules in the shared tier reach every cousin's packet in
    full; the rest of the tier is an index line each."""

    def _shared(self, name, text):
        (self.root / "shared").mkdir(exist_ok=True)
        (self.root / "shared" / name).write_text(text)

    def _section(self, text):
        start = text.index("## 2. Shared Rules and Fleet Memory")
        return text[start:text.index("## 3. Cousin Self-Portrait")]

    def test_rules_in_full_references_as_index_lines(self):
        self._shared("reference_first-principles.md",
                     "---\nname: fp\ndescription: think first\n"
                     "kind: rule\n---\nA cousin MUST decompose first.\n")
        self._shared("reference_lan-map.md",
                     "---\nname: net\ndescription: the LAN map\n---\n"
                     "the router, the switches and many details\n")
        section = self._section(assemble("wren", self.home)["text"])
        self.assertIn("A cousin MUST decompose first.", section)
        self.assertIn("- `reference_lan-map.md`: the LAN map", section)
        self.assertNotIn("many details", section)

    def test_pending_proposals_never_reach_the_packet(self):
        (self.root / "shared" / "proposed").mkdir(parents=True)
        (self.root / "shared" / "proposed" / "sam__x.md").write_text(
            "---\nkind: rule\n---\nUNREVIEWED RULE\n")
        text = assemble("wren", self.home)["text"]
        self.assertNotIn("UNREVIEWED RULE", text)

    def test_no_shared_tier_is_empty_and_not_degraded(self):
        result = assemble("wren", self.home)
        self.assertNotIn("shared", result["degraded_sections"])
        self.assertIn("## 2. Shared Rules and Fleet Memory", result["text"])
