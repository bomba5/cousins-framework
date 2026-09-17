"""assemble() runs the distiller before reading distilled files, so the
durable floor in every boot packet is fresh without any cousin habit or
timer: the consumer triggers the producer, like search self-heal."""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import boot, distill, memory


class BootDistillCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _plant(self, topic, content, truth_level="cousin-conclusion"):
        memory._append_raw(self.home, {"topic": topic, "content": content,
                                       "truth_level": truth_level,
                                       "source": "decision"})


class TestHook(BootDistillCase):
    def test_assemble_distills_raw_into_the_floor_before_reading(self):
        self._plant("retention window", "keep thirty days of flows")
        memory.ensure_layout(self.home)
        stub = (self.home / "memory" / "distilled" / "decisions.md").read_text()
        self.assertIn(memory.STUB_TEXT, stub)
        pkt = boot.assemble("testa", self.home, generation=1)
        text = (self.home / "memory" / "distilled" / "decisions.md").read_text()
        self.assertIn("keep thirty days of flows", text)
        self.assertIn("## decisions.md", pkt["text"])
        self.assertIn("keep thirty days of flows", pkt["text"])

    def test_marker_and_auto_header_are_not_packet_noise(self):
        self._plant("retention window", "keep thirty days of flows")
        pkt = boot.assemble("testa", self.home, generation=1)
        self.assertNotIn(distill.AUTO_MARKER, pkt["text"])
        self.assertNotIn(distill.AUTO_HEADER, pkt["text"])

    def test_stub_files_are_not_printed_and_calibration_stays_degraded(self):
        # A fresh home: distill writes six stubs, none of which is memory.
        pkt = boot.assemble("testa", self.home, generation=1)
        self.assertNotIn(memory.STUB_TEXT, pkt["text"])
        self.assertNotIn("## glossary.md", pkt["text"])
        self.assertIn("calibration", pkt["degraded_sections"])

    def test_operator_stated_raw_lifts_the_calibration_layer(self):
        self._plant("feedback: short statuses", "one line, then stop",
                    truth_level="operator-stated")
        pkt = boot.assemble("testa", self.home, generation=1)
        self.assertNotIn("calibration", pkt["degraded_sections"])
        self.assertIn("one line, then stop", pkt["text"])
        self.assertNotIn(distill.AUTO_MARKER, pkt["text"])

    def test_a_failing_distiller_never_breaks_the_boot(self):
        self._plant("t", "c")
        with mock.patch.object(distill, "distill",
                               side_effect=OSError("disk full")):
            pkt = boot.assemble("testa", self.home, generation=1)
        self.assertIn("BOOT PACKET FOR COUSIN: testa", pkt["text"])


if __name__ == "__main__":
    unittest.main()
