"""The transcript-size guard is gone from the loops daemon.

It measured the legacy lane's session transcript
(<transcripts_dir>/<[runtime] session_id>.jsonl) against
config/harness.toml flip_when_transcript_mb and queued a timed flip.
Both keys are 2.0.0's removed keys: a leftover is inert, so the daemon
neither measures nor flips on it. A runner rolls its own session over
on context pressure.
"""
import re
import time
import unittest
from pathlib import Path

from cousin_lib import config
from cousin_lib.loops import list_requests
from tests.test_loops import LoopsCase


class TestSizeGuardGone(LoopsCase):
    def test_a_leftover_threshold_and_a_big_transcript_queue_nothing(self):
        self._cousin("wren")
        (self.root / "config" / "harness.toml").write_text(
            'transcripts_dir = "%s/{home_encoded}"\nflip_when_transcript_mb = 1\n'
            % (self.root / "transcripts"))
        home = self.root / "cousins" / "wren"
        (home / "cousin.toml").write_text(
            (home / "cousin.toml").read_text() + '[runtime]\nsession_id = "sid-a"\n')
        encoded = re.sub(r"[^A-Za-z0-9]", "-", str(home))
        transcript = self.root / "transcripts" / encoded / "sid-a.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(b"x" * (2 * 1024 * 1024))
        report = self._tick(now=time.time())
        self.assertEqual([r for r in list_requests() if r["kind"] == "flip"], [])
        self.assertNotIn("guarded", report)
        self.assertFalse([e for e in report["errors"] if "size guard" in e], report["errors"])


class TestHarnessKey(unittest.TestCase):
    def _root(self, body):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "config").mkdir()
        (root / "config" / "harness.toml").write_text(body)
        return root

    def test_key_is_read_as_a_number(self):
        cfg = config.harness_config(self._root(
            "flip_when_transcript_mb = 150\n"))
        self.assertEqual(cfg["flip_when_transcript_mb"], 150)

    def test_key_absent_is_none(self):
        cfg = config.harness_config(self._root(
            'transcripts_dir = "/var/lib/h/{home_encoded}"\n'))
        self.assertIsNone(cfg["flip_when_transcript_mb"])

    def test_non_positive_or_non_numeric_is_loud(self):
        for body in ('flip_when_transcript_mb = "big"\n',
                     "flip_when_transcript_mb = 0\n",
                     "flip_when_transcript_mb = -5\n",
                     "flip_when_transcript_mb = true\n"):
            with self.assertRaises(config.MissingConfigError, msg=body):
                config.harness_config(self._root(body))


if __name__ == "__main__":
    unittest.main()
