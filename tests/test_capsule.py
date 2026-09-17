"""Reasoning capsules: the jsonl store, the markdown mirror under
memory/distilled, the newest-first listing, the CLI, and the two
integration points (boot's memories layer; the distiller leaving the
file alone).

The first block re-expresses the source framework's own unit tests for
this module (path, create-on-write, appendable, optional sections,
empty listing, newest N, rotation). Everything after is this tree's:
the jsonl round trip, the curated-region rule against the distill
marker, and the boot packet.
"""
import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cousin_lib import capsule, distill, memory
from cousin_lib.boot import assemble


class CapsuleCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()

    def _write(self, conclusion="X", evidence=("e1",), **kw):
        return capsule.write_capsule(
            self.home, conclusion=conclusion, evidence=list(evidence), **kw)


class TestPortedFromSource(CapsuleCase):
    def test_markdown_mirror_lives_under_distilled(self):
        path = capsule.markdown_path(self.home)
        self.assertEqual(
            path, self.home / "memory" / "distilled" / "reasoning-capsules.md")
        self.assertEqual(capsule.capsules_path(self.home),
                         self.home / "memory" / "capsules.jsonl")

    def test_write_creates_both_files_when_missing(self):
        out = self._write(conclusion="egress remarking is the right path",
                          evidence=["measured jitter improvement",
                                    "register confirmed via vendor SDK"])
        self.assertEqual(out, capsule.capsules_path(self.home))
        self.assertTrue(out.exists())
        self.assertTrue(capsule.markdown_path(self.home).exists())

    def test_two_writes_are_two_distinct_blocks(self):
        self._write(conclusion="X", evidence=["e1"])
        self._write(conclusion="Y", evidence=["e2"])
        ids = [c["id"] for c in capsule.list_capsules(self.home)]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        text = capsule.markdown_path(self.home).read_text()
        for cid in ids:
            self.assertIn(cid, text)

    def test_optional_sections_are_recorded(self):
        self._write(
            conclusion="ship the regex fix",
            evidence=["unit test reproduces the corruption"],
            rejected=["restore the old id from version control"],
            confidence="high",
            truth_level="cousin-conclusion",
            topic="session id corruption",
        )
        text = capsule.markdown_path(self.home).read_text()
        for needle in ("**Conclusion**", "**Evidence**",
                       "**Rejected alternatives**",
                       "_topic: session id corruption_",
                       "_confidence: high_",
                       "_truth_level: cousin-conclusion_"):
            self.assertIn(needle, text)
        entry = capsule.list_capsules(self.home)[0]
        self.assertEqual(entry["rejected"],
                         ["restore the old id from version control"])
        self.assertEqual(entry["topic"], "session id corruption")

    def test_list_is_empty_without_a_store(self):
        self.assertEqual(capsule.list_capsules(self.home), [])

    def test_list_returns_newest_n_first(self):
        for i in range(5):
            self._write(conclusion="point %d" % i, evidence=["e%d" % i])
        got = capsule.list_capsules(self.home, n=3)
        self.assertEqual([c["conclusion"] for c in got],
                         ["point 4", "point 3", "point 2"])

    def test_rotation_archives_older_blocks_and_keeps_the_tail(self):
        big = ["x" * 400] * 5
        for i in range(60):
            self._write(conclusion="c%d" % i, evidence=big)
        path = capsule.markdown_path(self.home)
        with mock.patch.object(capsule, "CAPSULES_ROTATE_BYTES", 10_000):
            self.assertIsNotNone(capsule._rotate_if_needed(path))
        live = path.read_text()
        self.assertEqual(len(capsule._blocks(live)),
                         capsule.CAPSULES_KEEP_TAIL)
        self.assertIn("\nc59\n", live)
        self.assertNotIn("\nc0\n", live)
        archives = list(path.parent.glob("reasoning-capsules-archive-*.md"))
        self.assertTrue(archives)
        self.assertIn("\nc0\n", archives[0].read_text())
        # The jsonl store is untouched by markdown rotation.
        self.assertEqual(len(capsule.list_capsules(self.home, n=100)), 60)


class TestStore(CapsuleCase):
    def test_jsonl_round_trip_carries_every_field(self):
        self._write(conclusion="cap at 8200", evidence=["room for hive"],
                    rejected=["unbounded"], confidence="low",
                    truth_level="operator-stated", topic="port range")
        line = capsule.capsules_path(self.home).read_text().splitlines()[0]
        entry = json.loads(line)
        self.assertEqual(entry["conclusion"], "cap at 8200")
        self.assertEqual(entry["evidence"], ["room for hive"])
        self.assertEqual(entry["rejected"], ["unbounded"])
        self.assertEqual(entry["confidence"], "low")
        self.assertEqual(entry["truth_level"], "operator-stated")
        self.assertEqual(entry["topic"], "port range")
        self.assertTrue(entry["id"].startswith("capsule-"))
        self.assertIn("T", entry["timestamp"])

    def test_defaults_are_medium_and_cousin_conclusion(self):
        self._write()
        entry = capsule.list_capsules(self.home)[0]
        self.assertEqual(entry["confidence"], "medium")
        self.assertEqual(entry["truth_level"], memory.DEFAULT_TRUTH_LEVEL)
        self.assertEqual(entry["rejected"], [])
        self.assertEqual(entry["topic"], "")

    def test_empty_conclusion_or_evidence_is_refused(self):
        with self.assertRaises(ValueError):
            self._write(conclusion="   ", evidence=["e"])
        with self.assertRaises(ValueError):
            self._write(conclusion="x", evidence=[])
        with self.assertRaises(ValueError):
            self._write(conclusion="x", evidence=["  "])
        self.assertFalse(capsule.capsules_path(self.home).exists())

    def test_unknown_confidence_is_refused(self):
        with self.assertRaises(ValueError):
            self._write(confidence="certain")

    def test_a_bad_jsonl_line_is_skipped_not_fatal(self):
        self._write(conclusion="good")
        with open(capsule.capsules_path(self.home), "a") as fh:
            fh.write("{not json\n\n")
        self._write(conclusion="also good")
        self.assertEqual([c["conclusion"] for c in
                          capsule.list_capsules(self.home)],
                         ["also good", "good"])


class TestCuratedRegion(CapsuleCase):
    def test_distill_leaves_the_capsules_file_byte_identical(self):
        # reasoning-capsules.md is not one of the six distilled files;
        # the distiller must neither rewrite nor stub it.
        self._write(conclusion="keep 30 days", evidence=["audits"])
        path = capsule.markdown_path(self.home)
        before = path.read_bytes()
        report = distill.distill(self.home)
        self.assertNotIn("reasoning-capsules.md", report["files"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                         sorted(memory.DISTILLED_FILES
                                + ("reasoning-capsules.md",)))

    def test_a_new_block_lands_above_an_existing_marker(self):
        path = capsule.markdown_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("# Reasoning Capsules\n\nhand note\n\n%s\n"
                        "%s: generated tail_\n\n- old line\n"
                        % (distill.AUTO_MARKER, distill.AUTO_HEADER))
        self._write(conclusion="above the line")
        text = path.read_text()
        self.assertLess(text.index("above the line"),
                        text.index(distill.AUTO_MARKER))
        self.assertIn("hand note", text)
        self.assertTrue(text.rstrip().endswith("- old line"))
        # And the same file survives a distill run.
        distill.distill(self.home)
        self.assertEqual(path.read_text(), text)


class TestBootPacket(CapsuleCase):
    def setUp(self):
        super().setUp()
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text("1. Law.\n")
        (self.home / "self-portrait.md").write_text(
            "# Cousin Self-Portrait: testa\n## Voice\nPlain.\n"
            "## Operator Calibration\nShort statuses.\n")
        (self.home / "STATUS.md").write_text(
            "# STATUS\n\n## Open loops\n\n- one\n")
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _memories_section(self, text):
        start = text.index("## 7. Retrieved Memories")
        end = text.index("## 8. Tool Surface")
        return text[start:end]

    def test_memories_carry_the_newest_five_conclusions(self):
        for i in range(7):
            self._write(conclusion="conclusion number %d" % i,
                        evidence=["e"], topic="t%d" % i)
        section = self._memories_section(
            assemble("testa", self.home)["text"])
        self.assertIn("## Reasoning capsules", section)
        for i in range(2, 7):
            self.assertIn("conclusion number %d" % i, section)
        for i in range(0, 2):
            self.assertNotIn("conclusion number %d" % i, section)
        self.assertLess(section.index("conclusion number 6"),
                        section.index("conclusion number 2"))
        self.assertIn("topic: t6", section)

    def test_no_capsules_means_no_block(self):
        section = self._memories_section(
            assemble("testa", self.home)["text"])
        self.assertNotIn("Reasoning capsules", section)

    def test_the_marker_never_reaches_the_packet(self):
        path = capsule.markdown_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("# Reasoning Capsules\n\n%s\n%s tail_\n"
                        % (distill.AUTO_MARKER, distill.AUTO_HEADER))
        self._write(conclusion="visible")
        text = assemble("testa", self.home)["text"]
        self.assertIn("visible", text)
        self.assertNotIn(distill.AUTO_MARKER, text)


class TestCli(CapsuleCase):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = capsule.reason_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_capsule_then_list_round_trip(self):
        rc, out, _ = self._run(
            "--home", str(self.home), "capsule",
            "--conclusion", "cap at 8200",
            "--evidence", "room for hive", "--evidence", "no collisions",
            "--rejected", "unbounded", "--confidence", "high",
            "--topic", "port range")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().startswith("capsule-"), out)
        rc, out, _ = self._run("--home", str(self.home), "list", "--n", "5")
        self.assertEqual(rc, 0)
        self.assertIn("cap at 8200", out)
        self.assertIn("room for hive", out)
        self.assertIn("unbounded", out)
        self.assertIn("[high]", out)
        self.assertIn("port range", out)

    def test_list_is_newest_first(self):
        self._write(conclusion="older")
        self._write(conclusion="newer")
        _, out, _ = self._run("--home", str(self.home), "list")
        self.assertLess(out.index("newer"), out.index("older"))

    def test_empty_store_lists_a_placeholder(self):
        rc, out, _ = self._run("--home", str(self.home), "list")
        self.assertEqual(rc, 0)
        self.assertIn("(no capsules)", out)

    def test_refuses_without_a_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COUSIN_HOME", None)
            rc, _, err = self._run("list")
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err)

    def test_empty_evidence_bullet_is_a_usage_error(self):
        rc, _, err = self._run("--home", str(self.home), "capsule",
                               "--conclusion", "x", "--evidence", "  ")
        self.assertEqual(rc, 2)
        self.assertIn("evidence", err)


if __name__ == "__main__":
    unittest.main()
