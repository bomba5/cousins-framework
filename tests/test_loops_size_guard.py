"""The transcript-size guard inside the loops daemon.

A harness session transcript grows without bound; the source framework
reached hundreds of megabytes before a separate timer script force-
flipped the worst offender. Here the guard is a tick step that submits
ONE timed-flip request through the daemon's own request store - the
same path an operator's timed flip takes, with the same warning ladder
- for at most one cousin per tick, and never a second while one is
pending for that cousin.
"""
import time
import unittest
from pathlib import Path

from cousin_lib import config
from cousin_lib.loops import list_requests, submit_request
from tests.test_loops import LoopsCase


class GuardCase(LoopsCase):
    def _harness(self, mb=1, transcripts=True):
        lines = []
        if transcripts:
            lines.append('transcripts_dir = "%s/{home_encoded}"\n'
                         % (self.root / "transcripts"))
        if mb is not None:
            lines.append("flip_when_transcript_mb = %s\n" % mb)
        (self.root / "config" / "harness.toml").write_text("".join(lines))

    def _session(self, slug, session_id, size_bytes):
        home = self.root / "cousins" / slug
        toml = (home / "cousin.toml").read_text()
        (home / "cousin.toml").write_text(
            toml + '[runtime]\nsession_id = "%s"\n' % session_id)
        encoded = str(home).replace("/", "-")
        transcript = (self.root / "transcripts" / encoded
                      / ("%s.jsonl" % session_id))
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_bytes(b"x" * size_bytes)
        return transcript

    def _pending_flips(self):
        return [r for r in list_requests(status="pending")
                if r["kind"] == "flip"]


class TestSizeGuard(GuardCase):
    def test_over_threshold_submits_one_timed_flip_request(self):
        self._cousin("wren")
        self._harness(mb=1)
        self._session("wren", "sid-a", 2 * 1024 * 1024)
        base = time.time()
        report = self._tick(now=base)
        rows = self._pending_flips()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cousin"], "wren")
        import json
        payload = json.loads(rows[0]["payload"])
        self.assertAlmostEqual(payload["fire_at"], base + 300, delta=1)
        self.assertIn("transcript over 1 MB", payload["reason"])
        self.assertEqual(report["guarded"], ["wren"])

    def test_under_threshold_submits_nothing(self):
        self._cousin("wren")
        self._harness(mb=1)
        self._session("wren", "sid-a", 512 * 1024)
        report = self._tick()
        self.assertEqual(self._pending_flips(), [])
        self.assertEqual(report["guarded"], [])

    def test_no_duplicate_while_a_flip_is_pending(self):
        self._cousin("wren")
        self._harness(mb=1)
        self._session("wren", "sid-a", 2 * 1024 * 1024)
        base = time.time()
        self._tick(now=base)
        self._tick(now=base + 30)
        self.assertEqual(len(self._pending_flips()), 1)

    def test_operator_pending_flip_also_counts(self):
        self._cousin("wren")
        self._harness(mb=1)
        self._session("wren", "sid-a", 2 * 1024 * 1024)
        submit_request("flip", cousin="wren",
                       payload={"fire_at": time.time() + 3600},
                       ttl_seconds=7200)
        report = self._tick()
        self.assertEqual(len(self._pending_flips()), 1)
        self.assertEqual(report["guarded"], [])

    def test_absent_seam_does_nothing(self):
        self._cousin("wren")
        self._session("wren", "sid-a", 2 * 1024 * 1024)
        report = self._tick()
        self.assertEqual(self._pending_flips(), [])
        self.assertEqual(report["guarded"], [])
        self.assertEqual(report["errors"], [])

    def test_key_absent_means_off(self):
        self._cousin("wren")
        self._harness(mb=None)
        self._session("wren", "sid-a", 2 * 1024 * 1024)
        report = self._tick()
        self.assertEqual(self._pending_flips(), [])
        self.assertEqual(report["errors"], [])

    def test_at_most_one_cousin_per_tick_largest_first(self):
        self._cousin("wren")
        self._cousin("toki")
        self._harness(mb=1)
        self._session("wren", "sid-a", 2 * 1024 * 1024)
        self._session("toki", "sid-b", 3 * 1024 * 1024)
        base = time.time()
        first = self._tick(now=base)
        self.assertEqual(first["guarded"], ["toki"])
        second = self._tick(now=base + 30)
        self.assertEqual(second["guarded"], ["wren"])
        self.assertEqual(len(self._pending_flips()), 2)

    def test_dead_cousin_and_no_session_id_are_skipped(self):
        self._cousin("wren")
        self._cousin("toki")
        self._harness(mb=1)
        self._session("wren", "sid-a", 2 * 1024 * 1024)
        # toki has no runtime.session_id: nothing to measure.
        report = self._tick(is_alive=lambda slug: slug != "wren")
        self.assertEqual(report["guarded"], [])
        self.assertEqual(self._pending_flips(), [])

    def test_worker_is_never_guarded(self):
        home = self._cousin("grinder")
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "grinder"\nname = "Grinder"\n'
            'type = "worker"\n[chat]\nport = 8101\n'
            '[heartbeat]\ncontext_beat_seconds = 0\n')
        self._harness(mb=1)
        self._session("grinder", "sid-w", 2 * 1024 * 1024)
        report = self._tick()
        self.assertEqual(report["guarded"], [])

    def test_threshold_without_transcripts_dir_is_reported(self):
        # A guard that cannot measure is a dead key: say so, loudly.
        self._cousin("wren")
        self._harness(mb=1, transcripts=False)
        report = self._tick()
        self.assertTrue(any("flip_when_transcript_mb" in e
                            and "transcripts_dir" in e
                            for e in report["errors"]))

    def test_missing_transcript_file_is_skipped_quietly(self):
        self._cousin("wren")
        self._harness(mb=1)
        transcript = self._session("wren", "sid-a", 2 * 1024 * 1024)
        transcript.unlink()
        report = self._tick()
        self.assertEqual(report["guarded"], [])
        self.assertEqual(report["errors"], [])


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
