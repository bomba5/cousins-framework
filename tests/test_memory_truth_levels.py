"""Truth levels on memory writes.

Without them every raw memory read L3: `decide` and the flip miner
hardcoded the cousin-conclusion level and nothing let a cousin record
what the operator said, although the boot law asks for exactly that
with a cited source. `decide` and the new
`remember` take --level; operator-stated needs --cite; the distiller
routes the canonical L0 name to operator-calibration.md.
"""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import distill, memory
from cousin_lib.memory import memory_main


class LevelCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def raw(self):
        rows = []
        for f in sorted((self.home / "memory" / "raw").glob("*.jsonl")):
            rows += [json.loads(l) for l in f.read_text().splitlines() if l]
        return rows


class TestLevels(LevelCase):
    def test_decide_defaults_to_the_canonical_conclusion(self):
        # enforces: law 10
        rc, _, _ = self.run_cli("decide", "t", "d", "why")
        self.assertEqual(rc, 0)
        self.assertEqual(self.raw()[-1]["truth_level"], "L3_COUSIN_CONCLUSION")

    def test_decide_takes_a_level(self):
        rc, _, _ = self.run_cli("decide", "t", "d", "why", "--level", "hypothesis")
        self.assertEqual(rc, 0)
        self.assertEqual(self.raw()[-1]["truth_level"], "L4_COUSIN_HYPOTHESIS")

    def test_operator_level_without_a_citation_is_refused(self):
        # enforces: law 10
        # stricter than the law's demotion: refused, not demoted
        rc, _, err = self.run_cli("remember", "tone", "keep it short",
                                  "--level", "operator")
        self.assertEqual(rc, 2)
        self.assertIn("--cite", err)
        self.assertEqual(self.raw(), [])

    def test_remember_stores_an_operator_fact_with_its_source(self):
        rc, _, _ = self.run_cli("remember", "tone", "keep it short",
                                "--level", "operator", "--cite", "chat 42")
        self.assertEqual(rc, 0)
        row = self.raw()[-1]
        self.assertEqual(row["truth_level"], "L0_OPERATOR")
        self.assertEqual(row["cite"], "chat 42")
        self.assertEqual(row["source"], "remember")

    def test_an_uncited_framework_or_tool_level_is_demoted_to_conclusion(self):
        # enforces: law 10
        for level, canonical in (("framework", "L1_FRAMEWORK"), ("tool", "L2_TOOL"),
                                 ("L2_TOOL", "L2_TOOL")):
            with self.subTest(level=level):
                rc, out, _ = self.run_cli("remember", "probe", "Kestrel answered",
                                          "--level", level)
                self.assertEqual(rc, 0)
                self.assertEqual(self.raw()[-1]["truth_level"], "L3_COUSIN_CONCLUSION")
                [note] = [l for l in out.splitlines() if l.startswith("demoted:")]
                self.assertIn(canonical, note)
                self.assertIn("--cite", note)
        rc, out, _ = self.run_cli("decide", "probe", "retry", "it timed out",
                                  "--level", "tool")
        self.assertEqual(rc, 0)
        self.assertEqual(self.raw()[-1]["truth_level"], "L3_COUSIN_CONCLUSION")
        self.assertIn("demoted: L2_TOOL needs --cite", out)

    def test_a_cited_framework_or_tool_level_keeps_it_and_says_nothing(self):
        # enforces: law 10
        for level, canonical in (("framework", "L1_FRAMEWORK"), ("tool", "L2_TOOL")):
            with self.subTest(level=level):
                rc, out, _ = self.run_cli("remember", "probe", "Kestrel answered",
                                          "--level", level, "--cite", "ping, 2026-09-30")
                self.assertEqual(rc, 0)
                self.assertEqual(self.raw()[-1]["truth_level"], canonical)
                self.assertNotIn("demoted", out)

    def test_the_tool_path_says_it_too(self):
        line = memory.remember(self.home, "probe", "Kestrel answered", level="framework")
        self.assertEqual(line.splitlines()[1],
                         "demoted: L1_FRAMEWORK needs --cite (a cited source, law 10);"
                         " written as L3_COUSIN_CONCLUSION")
        self.assertEqual(self.raw()[-1]["truth_level"], "L3_COUSIN_CONCLUSION")

    def test_the_frameworks_own_entries_keep_their_level(self):
        # enforces: law 10
        # the framework's own writes are not the cousin's to demote
        self.assertTrue(memory.record_event(self.home, "framework", "framework:rollover",
                                            "generation 4 started", "runner"))
        self.assertTrue(memory.record_event(self.home, "L2_TOOL", "job:fleet-sync",
                                            "job #3 done (exit 0): fleet sync", "job"))
        rows = self.raw()
        self.assertEqual([r["truth_level"] for r in rows], ["L1_FRAMEWORK", "L2_TOOL"])
        self.assertTrue(all("cite" not in r for r in rows))

    def test_an_unknown_level_is_refused(self):
        rc, _, err = self.run_cli("remember", "t", "f", "--level", "gospel")
        self.assertEqual(rc, 2)
        self.assertIn("unknown truth level", err)

    def test_normalize_maps_short_and_canonical_names(self):
        self.assertEqual(memory.normalize_level("operator-stated"), "L0_OPERATOR")
        self.assertEqual(memory.normalize_level("L0_OPERATOR"), "L0_OPERATOR")
        self.assertEqual(memory.normalize_level(None), "L3_COUSIN_CONCLUSION")
        self.assertEqual(memory.normalize_level("nonsense"), "other")

    def test_distill_routes_both_operator_names_to_calibration(self):
        for level in ("L0_OPERATOR", "operator-stated"):
            self.assertEqual(distill.classify({"topic": "misc", "truth_level": level}),
                             "operator-calibration.md")


if __name__ == "__main__":
    unittest.main()
