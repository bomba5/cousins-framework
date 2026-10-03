"""Corrections capture: detection classes, the jsonl store, the boot
summary, and the two integration points (chat server on an operator
send; the state digest's calibration layer).

The detection tests are re-expressed from an earlier version's own
unit tests for this module; the patterns are generic English and carry
nothing private. The store here is per-cousin (home/data), so the
source's cousin-filter tests have no counterpart.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import corrections
from cousin_lib.config import CousinConfig
from cousin_lib.runner import prompt
from cousin_lib.server import chat_api


class TestDetect(unittest.TestCase):
    def test_each_class_from_a_sample_line(self):
        samples = {
            "don't push to the upstream repos": "negative_directive",
            "you shouldn't do that": "negative_directive",
            "do not touch that file": "negative_directive",
            "stop, that is enough": "halt",
            "No, that's the wrong approach.": "rejection",
            "actually the port is 8081": "soft_correction",
            "try the registry approach instead": "redirect",
            "perfect, that's exactly what I wanted": "acceptance",
            "I want the summary first": "preference_positive",
            "I notice you didn't run the audit step": "meta_correction",
            "the tone is too formal for this thread": "style_coaching",
            "you should read the spec first": "directive",
        }
        for text, kind in samples.items():
            self.assertEqual(corrections.detect(text), kind, text)

    def test_neutral_text_is_none(self):
        self.assertIsNone(corrections.detect(
            "The rack temperature is 24 degrees Celsius today."))

    def test_empty_input_is_none(self):
        self.assertIsNone(corrections.detect(""))
        self.assertIsNone(corrections.detect(None))

    def test_acceptance_needs_a_whole_short_ok_not_a_substring(self):
        self.assertEqual(corrections.detect("ok"), "acceptance")
        self.assertEqual(corrections.detect("yes, ship it"), "acceptance")
        self.assertIsNone(corrections.detect("the okra is in the fridge"))

    def test_correction_outranks_acceptance_in_one_line(self):
        # "good, but don't ..." is a correction, not applause.
        self.assertEqual(
            corrections.detect("good, but don't repeat the header"),
            "negative_directive")


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)


class TestRecordAndSummary(StoreCase):
    def test_record_appends_jsonl_under_data(self):
        corrections.record(self.home, user="Sam",
                           text="don't include that", kind="negative_directive")
        path = self.home / "data" / "corrections.jsonl"
        self.assertTrue(path.is_file())
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user"], "Sam")
        self.assertEqual(rows[0]["kind"], "negative_directive")
        self.assertEqual(rows[0]["text"], "don't include that")
        self.assertIn("ts", rows[0])

    def test_summary_empty_marker_when_nothing_recorded(self):
        self.assertEqual(corrections.summary_for_boot(self.home),
                         "(no corrections recorded yet)")

    def test_summary_newest_first_and_bounded(self):
        for i in range(20):
            corrections.record(self.home, user="Sam",
                               text="don't do thing %02d" % i,
                               kind="negative_directive")
        out = corrections.summary_for_boot(self.home, n=15)
        lines = out.splitlines()
        self.assertEqual(lines[0],
                         "# Recent operator corrections (last 15)")
        self.assertEqual(len(lines), 16)
        self.assertIn("thing 19", lines[1])
        self.assertIn("thing 05", lines[-1])
        self.assertNotIn("thing 04", out)

    def test_summary_truncates_to_140_and_flattens_newlines(self):
        long = "don't " + "x" * 300 + "\nsecond line"
        corrections.record(self.home, user="Sam", text=long,
                           kind="negative_directive")
        line = corrections.summary_for_boot(self.home).splitlines()[1]
        self.assertNotIn("\n", line)
        self.assertLessEqual(len(line), len('- [negative_directive] ""') + 140)
        self.assertTrue(line.startswith('- [negative_directive] "don\'t '))

    def test_malformed_lines_are_skipped(self):
        path = self.home / "data" / "corrections.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n")
        corrections.record(self.home, user="Sam", text="stop",
                           kind="halt")
        out = corrections.summary_for_boot(self.home)
        self.assertIn("[halt]", out)
        self.assertEqual(len(out.splitlines()), 2)

    def test_detect_and_record_returns_kind_or_none(self):
        self.assertIsNone(corrections.detect_and_record(
            self.home, user="Sam", text="the sky is blue"))
        self.assertFalse(
            (self.home / "data" / "corrections.jsonl").exists())
        self.assertEqual(corrections.detect_and_record(
            self.home, user="Sam", text="stop"), "halt")
        self.assertTrue(
            (self.home / "data" / "corrections.jsonl").exists())


class TestDigestCalibration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory" / "distilled").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text("1. Law.\n")
        (self.home / "memory" / "distilled" / "operator-calibration.md").write_text(
            "Short statuses.\n")
        (self.home / "STATUS.md").write_text(
            "# STATUS\n\n## Open loops\n\n- one\n")
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _digest(self):
        return prompt.state_digest(self.home, root=self.root, slug="testa",
                                   generation=1)

    def _calibration_section(self, text):
        start = text.index("Operator Calibration")
        end = text.index("Active State")
        return text[start:end]

    def test_calibration_carries_the_summary_when_present(self):
        corrections.record(self.home, user="Sam",
                           text="don't lead with the caveat",
                           kind="negative_directive")
        digest = self._digest()
        section = self._calibration_section(digest["text"])
        self.assertIn("Short statuses.", section)
        self.assertIn("# Recent operator corrections (last 1)", section)
        self.assertIn("lead with the caveat", section)
        self.assertNotIn("calibration", digest["degraded_sections"])

    def test_no_corrections_leaves_calibration_untouched(self):
        section = self._calibration_section(self._digest()["text"])
        self.assertNotIn("Recent operator corrections", section)
        self.assertNotIn("no corrections recorded", section)

    def test_no_distilled_calibration_still_surfaces_corrections(self):
        # Recorded corrections are calibration data and must not vanish
        # with a missing distilled calibration.
        (self.home / "memory" / "distilled" / "operator-calibration.md").unlink()
        corrections.record(self.home, user="Sam", text="stop", kind="halt")
        self.assertIn("[halt]", self._calibration_section(self._digest()["text"]))


class TestChatSendRecords(unittest.TestCase):
    """The in-process chat send (chat_api.send: the console, cousin-chat,
    the bridge) captures an operator's correction; no chat server runs."""

    def _boot(self, toml_extra=""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n' + toml_extra)
        self.home = home
        return CousinConfig.load(home)

    def _send(self, config, user, message):
        out = chat_api.send(config, {"user": user, "message": message})
        return 200 if out.get("ok") else 500

    def _rows(self):
        path = self.home / "data" / "corrections.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines()]

    def test_operator_correction_is_recorded(self):
        server = self._boot('[operator]\nname = "Sam"\n')
        self.assertEqual(self._send(server, "Sam", "don't do that"), 200)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "negative_directive")
        self.assertEqual(rows[0]["user"], "Sam")

    def test_operator_match_is_case_insensitive(self):
        server = self._boot('[operator]\nname = "Sam"\n')
        self._send(server, "sam", "stop")
        self.assertEqual([r["kind"] for r in self._rows()], ["halt"])

    def test_operator_neutral_message_records_nothing(self):
        server = self._boot('[operator]\nname = "Sam"\n')
        self._send(server, "Sam", "the build finished")
        self.assertEqual(self._rows(), [])

    def test_non_operator_sender_records_nothing(self):
        server = self._boot('[operator]\nname = "Sam"\n')
        self._send(server, "Peer", "don't do that")
        self.assertEqual(self._rows(), [])

    def test_no_configured_operator_records_nothing(self):
        server = self._boot()
        self.assertEqual(self._send(server, "Sam", "don't do that"), 200)
        self.assertEqual(self._rows(), [])

    def test_a_failing_recorder_never_fails_the_send(self):
        server = self._boot('[operator]\nname = "Sam"\n')
        with mock.patch.object(corrections, "record",
                               side_effect=OSError("disk full")):
            self.assertEqual(self._send(server, "Sam", "stop"), 200)


if __name__ == "__main__":
    unittest.main()
