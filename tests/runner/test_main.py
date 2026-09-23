"""cousin-runner: the process a cousin lives in."""
import contextlib
import io
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from cousin_lib.delivery import Item
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.fake import FakeRunner
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _append_agent_key(home, name):
    """Add `api_key_file = "<name>"` under the `[agent]` table `temp_home`
    already wrote (it's the last line in the file, so a bare append stays
    inside that table)."""
    (home / "cousin.toml").write_text(
        (home / "cousin.toml").read_text() + 'api_key_file = "%s"\n' % name)


class TestRunnerFor(HermeticCase):
    def test_reads_the_runner_kind_from_cousin_toml(self):
        home = temp_home(self, runner="fake")
        r = runner_main.runner_for(home)
        self.assertEqual(type(r).__name__, "FakeRunner")

    def test_an_unknown_runner_is_an_error_that_names_the_key(self):
        home = temp_home(self, runner="carrier-pigeon")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = runner_main.runner_main(["--home", str(home)])
        self.assertEqual(rc, 2)
        self.assertIn("runner", stderr.getvalue())


class TestReadKey(HermeticCase):
    def test_a_key_file_outside_any_framework_root_is_a_config_error(self):
        home = temp_home(self, runner="sdk")
        _append_agent_key(home, "keys/token")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = runner_main.runner_main(["--home", str(home)])
        self.assertEqual(rc, 2)
        self.assertIn("api_key_file", stderr.getvalue())

    def test_a_missing_key_file_under_a_real_root_is_a_config_error(self):
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir()
        _append_agent_key(home, "keys/token")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = runner_main.runner_main(["--home", str(home)])
        self.assertEqual(rc, 2)
        self.assertIn("keys/token", stderr.getvalue())


class TestSignalHandlers(HermeticCase):
    def test_signal_handlers_are_restored_when_start_fails(self):
        home = temp_home(self, runner="fake")
        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)
        with mock.patch.object(FakeRunner, "start",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_term)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous_int)


class TestOnce(HermeticCase):
    def test_once_drains_the_inbox_and_exits_zero(self):
        home = temp_home(self, runner="fake")
        Inbox(home).put(Item("operator:priya", "chat", "a", sender="Priya"))
        Inbox(home).put(Item("operator:priya", "chat", "b", sender="Priya"))
        rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 0)
        self.assertEqual(Inbox(home).pending(), 0)

    def test_once_recovers_a_claim_left_by_a_dead_runner(self):
        home = temp_home(self, runner="fake")
        inbox_id = Inbox(home).put(
            Item("operator:priya", "chat", "a", sender="Priya"))
        Inbox(home).claim(claimant="pid:99999")  # a runner that died holding the claim
        rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 0)
        self.assertEqual(Inbox(home).get(inbox_id)["state"], "done")


class TestSigterm(HermeticCase):
    def test_sigterm_stops_the_process_cleanly(self):
        home = temp_home(self, runner="fake")
        proc = subprocess.Popen([sys.executable, "-m", "cousin_lib.runner.main",
                                 "--home", str(home)],
                                cwd=os.getcwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.6)
            self.assertIsNone(proc.poll())
            Inbox(home).put(Item("operator:priya", "chat", "x", sender="Priya"))
            from cousin_lib.runner import wake
            wake.poke(home)
            time.sleep(0.4)
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(10)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(rc, 0)
        self.assertEqual(Inbox(home).pending(), 0)


if __name__ == "__main__":
    unittest.main()
