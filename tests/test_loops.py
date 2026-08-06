"""The loops daemon: docs/loops-spec.md as executable contract.

The first test is the one the SOURCE architecture cannot pass: a
request written by a different process, consumed by the daemon, its
status visible end to end. The source's bug was a write nobody reads
- state living in two processes - and if that test can pass with
split state, it is not testing the thing that is broken today.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.loops import (
    daemon_status,
    expire_stale_requests,
    list_requests,
    load_cousin_loops,
    submit_request,
    tick,
)


class LoopsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.delivered = []

    def _cousin(self, slug="wren", loops_toml="", extra=""):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n'
            '[chat]\nport = 8100\n%s%s'
            % (slug, slug.capitalize(), loops_toml, extra))
        return home

    def _deliver(self, slug, text):
        self.delivered.append((slug, text))
        return True

    def _tick(self, **kw):
        kw.setdefault("deliver", self._deliver)
        kw.setdefault("is_alive", lambda slug: True)
        return tick(**kw)


class TestCrossProcessRequests(LoopsCase):
    def test_request_written_by_another_process_is_consumed_here(self):
        # The write happens in a REAL separate interpreter - the shape
        # the source could not serve, because its request target was a
        # module dict in whichever process handled the HTTP call.
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "report"\ninterval_seconds = 99999\n'
            'prompt = "write the report"\n'))
        writer = subprocess.run(
            [sys.executable, "-c",
             "from cousin_lib.loops import submit_request;"
             "print(submit_request('fire', cousin='wren',"
             " payload={'loop': 'report'}))"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(
                pathlib.Path(__file__).resolve().parents[1])},
        )
        self.assertEqual(writer.returncode, 0, writer.stderr)
        request_id = int(writer.stdout.strip())
        pending = list_requests(status="pending")
        self.assertEqual([r["id"] for r in pending], [request_id])
        self._tick()
        self.assertTrue(any("write the report" in text
                            for _, text in self.delivered))
        row = list_requests()[0]
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["consumed_at"])

    def test_unconsumed_request_expires_loudly_never_drops(self):
        self._cousin("wren")
        rid = submit_request("fire", cousin="wren",
                             payload={"loop": "x"}, ttl_seconds=0)
        time.sleep(0.02)
        expired = expire_stale_requests()
        self.assertEqual(expired, 1)
        row = list_requests()[0]
        self.assertEqual(row["status"], "expired")
        self.assertIn("missed ticks", row["reason"])
        self.assertEqual(rid, row["id"])


class TestDaemonAbsenceIsLoud(LoopsCase):
    def test_never_run_is_reported(self):
        status = daemon_status()
        self.assertFalse(status["ok"])
        self.assertIn("never run", status["message"])

    def test_stale_tick_is_reported_with_age(self):
        self._cousin("wren")
        self._tick(now=time.time() - 500)
        status = daemon_status(tick_interval=30)
        self.assertFalse(status["ok"])
        self.assertIn("down", status["message"])
        self.assertIn("s ago", status["message"])

    def test_fresh_tick_is_ok(self):
        self._cousin("wren")
        self._tick()
        self.assertTrue(daemon_status(tick_interval=30)["ok"])


class TestLoopConfig(LoopsCase):
    def test_valid_loops_parse(self):
        home = self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "daily-report"\ndaily_at = "06:00"\n'
            'days = ["mon", "fri"]\nprompt = "report"\n'))
        loops, errors = load_cousin_loops(home)
        self.assertEqual(errors, [])
        self.assertEqual(loops[0]["name"], "daily-report")

    def test_enabled_zero_disables_truthiness_not_identity(self):
        home = self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "off"\ninterval_seconds = 60\n'
            'prompt = "x"\nenabled = 0\n'))
        loops, _ = load_cousin_loops(home)
        self.assertEqual(loops, [])

    def test_two_schedule_forms_is_an_error_not_silently_dead(self):
        home = self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "confused"\ninterval_seconds = 60\n'
            'daily_at = "06:00"\nprompt = "x"\n'))
        loops, errors = load_cousin_loops(home)
        self.assertEqual(loops, [])
        self.assertIn("exactly one schedule form", errors[0])

    def test_malformed_toml_names_the_cousin_never_silent(self):
        home = self._cousin("wren")
        (home / "cousin.toml").write_text("[cousin\nbroken")
        loops, errors = load_cousin_loops(home)
        self.assertEqual(loops, [])
        self.assertTrue(errors)
        self.assertIn(str(home), errors[0])


if __name__ == "__main__":
    unittest.main()
