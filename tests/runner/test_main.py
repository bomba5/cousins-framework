"""cousin-runner: the process a cousin lives in."""
import os
import signal
import subprocess
import sys
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class TestRunnerFor(HermeticCase):
    def test_reads_the_runner_kind_from_cousin_toml(self):
        home = temp_home(self, runner="fake")
        r = runner_main.runner_for(home)
        self.assertEqual(type(r).__name__, "FakeRunner")

    def test_an_unknown_runner_is_an_error_that_names_the_key(self):
        home = temp_home(self, runner="carrier-pigeon")
        with self.assertRaises(SystemExit) as cm:
            runner_main.runner_main(["--home", str(home)])
        self.assertEqual(cm.exception.code, 2)


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
