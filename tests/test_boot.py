"""The boot module: the generation counter and its start record, the
truncation marker, and the shared tier's rules and index.

The layers' composition, the total ceiling and degraded detection are
pinned on the state digest that reads them (tests/runner/test_digest.py).
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.boot import (
    _truncate,
    bump_generation,
    generation_started,
    mark_generation_start,
    read_generation,
    shared_parts,
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
        # A marker appended beyond the slice re-triggers the overflow
        # loop on the same victim forever - the flip-hang incident.
        out = _truncate("x" * 500, 100, "layer")
        self.assertLessEqual(len(out), 100)
        self.assertIn("truncated", out)


class TestSharedParts(BootCase):
    """Operator rules in the shared tier are quoted in full; the rest of
    the tier is an index line each."""

    def _shared(self, name, text):
        (self.root / "shared").mkdir(exist_ok=True)
        (self.root / "shared" / name).write_text(text)

    def test_rules_in_full_references_as_index_lines(self):
        self._shared("reference_first-principles.md",
                     "---\nname: fp\ndescription: think first\n"
                     "kind: rule\n---\nA cousin MUST decompose first.\n")
        self._shared("reference_lan-map.md",
                     "---\nname: net\ndescription: the LAN map\n---\n"
                     "the router, the switches and many details\n")
        rules, index = shared_parts(self.root)
        self.assertEqual(rules, ["### reference_first-principles\n"
                                 "A cousin MUST decompose first."])
        self.assertEqual(index, ["- `reference_lan-map.md`: the LAN map"])

    def test_pending_proposals_are_never_read(self):
        (self.root / "shared" / "proposed").mkdir(parents=True)
        (self.root / "shared" / "proposed" / "sam__x.md").write_text(
            "---\nkind: rule\n---\nUNREVIEWED RULE\n")
        self.assertEqual(shared_parts(self.root), ([], []))

    def test_no_shared_tier_is_empty(self):
        self.assertEqual(shared_parts(self.root), ([], []))


if __name__ == "__main__":
    unittest.main()
