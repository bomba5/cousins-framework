"""A runner killed with -9 loses no row: whatever it had not finished is
recovered by the next start."""
import os
import signal
import subprocess
import sys
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class TestKillNine(HermeticCase):
    def test_no_row_is_lost_when_the_runner_dies_with_sigkill(self):
        home = temp_home(self, runner="fake")
        inbox = Inbox(home)
        first = inbox.put(Item("operator:priya", "chat", "before the crash", sender="Priya"))
        proc = subprocess.Popen([sys.executable, "-m", "cousin_lib.runner.main", "--home", str(home)],
                                cwd=os.getcwd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        proc.kill()                       # SIGKILL: no stop(), no done()
        proc.wait(5)
        second = inbox.put(Item("operator:priya", "chat", "after the crash", sender="Priya"))
        # Read state before requeue_stale mutates it: a fake turn can finish
        # in well under the 0.5s kill delay, so `first` may already be done.
        not_done = [i for i in (first, second) if inbox.get(i)["state"] != "done"]
        # The claimed-then-died window itself is proven deterministically by
        # tests/runner/test_main.py::TestOnce::test_once_recovers_a_claim_left_by_a_dead_runner;
        # this test only needs every not-done row accounted for here.
        recovered = inbox.requeue_stale(older_than_s=0.0)
        self.assertEqual(recovered + inbox.pending(), len(not_done))
        self.assertEqual(inbox.unfinished(), len(not_done))
        rc = subprocess.call([sys.executable, "-m", "cousin_lib.runner.main", "--home", str(home), "--once"],
                             cwd=os.getcwd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(rc, 0)
        self.assertEqual(inbox.get(first)["state"], "done")
        self.assertEqual(inbox.get(second)["state"], "done")
        self.assertEqual(inbox.pending(), 0)
        self.assertEqual(inbox.unfinished(), 0)


if __name__ == "__main__":
    unittest.main()
