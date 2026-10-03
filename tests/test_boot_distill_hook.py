"""The state digest runs the distiller before reading distilled files, so
the durable floor in every boot is fresh without any cousin habit or
timer: the consumer triggers the producer, like search self-heal."""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import distill, memory
from cousin_lib.runner import prompt


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

    def _digest(self):
        return prompt.state_digest(self.home, root=self.root, slug="testa",
                                   generation=1)


class TestHook(BootDistillCase):
    def test_the_digest_distills_raw_into_the_floor_before_reading(self):
        self._plant("retention window", "keep thirty days of flows")
        memory.ensure_layout(self.home)
        stub = (self.home / "memory" / "distilled" / "decisions.md").read_text()
        self.assertIn(memory.STUB_TEXT, stub)
        digest = self._digest()
        text = (self.home / "memory" / "distilled" / "decisions.md").read_text()
        self.assertIn("keep thirty days of flows", text)
        self.assertIn("## decisions.md", digest["text"])
        self.assertIn("keep thirty days of flows", digest["text"])

    def test_marker_and_auto_header_are_not_digest_noise(self):
        self._plant("retention window", "keep thirty days of flows")
        digest = self._digest()
        self.assertNotIn(distill.AUTO_MARKER, digest["text"])
        self.assertNotIn(distill.AUTO_HEADER, digest["text"])

    def test_stub_files_are_not_printed(self):
        # A fresh home: distill writes six stubs, none of which is memory.
        digest = self._digest()
        self.assertNotIn(memory.STUB_TEXT, digest["text"])
        self.assertNotIn("## glossary.md", digest["text"])
        self.assertNotIn("Operator Calibration", digest["text"])

    def test_operator_stated_raw_reaches_the_calibration_layer(self):
        self._plant("feedback: short statuses", "one line, then stop",
                    truth_level="operator-stated")
        digest = self._digest()
        self.assertIn("Operator Calibration", digest["text"])
        self.assertIn("one line, then stop", digest["text"])
        self.assertNotIn(distill.AUTO_MARKER, digest["text"])

    def test_a_failing_distiller_never_breaks_the_boot(self):
        self._plant("t", "c")
        with mock.patch.object(distill, "distill",
                               side_effect=OSError("disk full")):
            digest = self._digest()
        self.assertIn("STATE DIGEST FOR COUSIN: testa", digest["text"])


if __name__ == "__main__":
    unittest.main()
