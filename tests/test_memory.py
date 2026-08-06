"""Memory primitives: decisions, activity, recall, consolidation.

The rule the whole module descends from: no cousin context, no
operation. A defaulted home silently reads and writes somebody else's
memory - the leak class that shaped the config seam.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.memory import memory_main


class MemoryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
        )
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestContext(MemoryCase):
    def test_no_context_is_a_refusal_not_a_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc, _, err = self._main(["activity", "doing things"])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err)


class TestDecide(MemoryCase):
    def test_appends_a_structured_entry(self):
        rc, out, _ = self._main(
            ["decide", "port rule", "claimed set excludes all", "safety"]
        )
        self.assertEqual(rc, 0)
        lines = (self.home / "data" / "decisions.jsonl").read_text()
        entry = json.loads(lines.strip())
        self.assertEqual(entry["topic"], "port rule")
        self.assertIn("timestamp", entry)

    def test_every_decision_also_lands_in_raw_memory(self):
        # The producer bridge: boot packets and the durable-memory audit
        # need memory/raw to have a writer, or the audit is pure noise.
        self._main(["decide", "t", "d", "r"])
        raw = list((self.home / "memory" / "raw").glob("*.jsonl"))
        self.assertEqual(len(raw), 1)
        entry = json.loads(raw[0].read_text().strip())
        self.assertEqual(entry["source"], "decision")
        self.assertIn("d - why: r", entry["content"])

    def test_rotation_keeps_the_newest_tail_live(self):
        decisions = self.home / "data" / "decisions.jsonl"
        decisions.parent.mkdir(parents=True)
        filler = json.dumps({"topic": "old", "decision": "x" * 4000,
                             "reasoning": ""})
        with open(decisions, "w") as fh:
            for _ in range(300):
                fh.write(filler + "\n")
        self._main(["decide", "fresh", "kept", "why"])
        remaining = decisions.read_text().splitlines()
        self.assertLessEqual(len(remaining), 201)
        self.assertIn("fresh", remaining[-1])
        archives = list((self.home / "data").glob(
            "decisions-archive-*.jsonl"))
        self.assertEqual(len(archives), 1)


class TestRecallAndActivity(MemoryCase):
    def test_recall_filters_by_keyword(self):
        self._main(["decide", "ports", "exclude claimed", "safety"])
        self._main(["decide", "naming", "wren stays", "collision-free"])
        rc, out, _ = self._main(["recall", "ports"])
        self.assertIn("exclude claimed", out)
        self.assertNotIn("wren stays", out)

    def test_activity_overwrites_the_checkpoint(self):
        self._main(["activity", "building", "the", "thing"])
        self._main(["activity", "reviewing it"])
        text = (self.home / "data" / "last-activity.txt").read_text()
        self.assertIn("reviewing it", text)
        self.assertNotIn("building", text)


class TestConsolidate(MemoryCase):
    def test_topics_with_three_entries_are_candidates(self):
        for i in range(3):
            self._main(["decide", "gate design", "iteration %d" % i, "r"])
        self._main(["decide", "one-off", "single", "r"])
        rc, out, _ = self._main(["consolidate"])
        self.assertIn("gate design", out)
        self.assertNotIn("one-off", out)

    def test_no_candidates_says_so(self):
        rc, out, _ = self._main(["consolidate"])
        self.assertIn("no promotion candidates", out)


if __name__ == "__main__":
    unittest.main()
