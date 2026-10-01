"""The out-of-process interrupt: an `interrupt` inbox row. The
contract suite proves both runners honour it; this module pins the seams
around it: its priority, what the delivery facade answers a caller that
waits (the console's route), and that a tmux cousin never gets one typed
into its pane."""
import os
import pathlib
import stat
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import delivery
from cousin_lib.runner.base import INTERRUPT, NO_TURN, SOURCE_PRIORITY, priority
from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

_FAKE_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
exit 0
"""


def _interrupt():
    return delivery.Item("system", INTERRUPT, "interrupt asked from the console", sender="Priya")


class TestPriority(unittest.TestCase):
    def test_an_interrupt_is_claimed_ahead_of_everything(self):
        self.assertEqual(priority(INTERRUPT, "system"), 0)
        self.assertEqual(min(SOURCE_PRIORITY.values()), 0)
        self.assertIn(INTERRUPT, delivery.SOURCES)


class TestThroughTheFacade(HermeticCase):
    """What the console's route does: deliver(wait=True) and read the outcome."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[agent]\nrunner = "fake"\n')

    def test_a_live_turn_is_interrupted_and_the_caller_hears_delivered(self):
        r = FakeRunner(self.home, turn_seconds=3.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(delivery.Item("operator:priya", "chat", "slow", sender="Priya"))
        deadline = time.monotonic() + 5
        while r.state() != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(delivery.deliver(self.home, _interrupt(), wait=True, timeout=3.0),
                         delivery.DELIVERED)

    def test_no_turn_running_is_failed(self):
        r = FakeRunner(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        self.assertEqual(delivery.deliver(self.home, _interrupt(), wait=True, timeout=3.0),
                         delivery.FAILED)
        [row] = [row for row in (r.inbox.get(i) for i in range(1, 5)) if row]
        self.assertEqual(row["detail"], NO_TURN)

    def test_no_runner_is_queued_never_delivered(self):
        self.assertEqual(delivery.deliver(self.home, _interrupt(), wait=True, timeout=0.3),
                         delivery.QUEUED)


class TestTmuxCousin(unittest.TestCase):
    def test_an_interrupt_is_never_typed_into_a_pane(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        home = root / "cousins" / "wren"
        (home / "data").mkdir(parents=True)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n'
                                          '[chat]\nport = 8099\n')
        tmux = root / "tmux"
        tmux.write_text(_FAKE_TMUX)
        tmux.chmod(tmux.stat().st_mode | stat.S_IEXEC)
        log = root / "calls.log"
        with mock.patch.dict(os.environ, {"FAKE_TMUX_LOG": str(log)}):
            out = delivery.deliver(home, _interrupt(), tmux_bin=str(tmux),
                                   settle=lambda n: 0, verify_delay=0)
        self.assertEqual(out, delivery.FAILED)
        self.assertFalse(log.exists(), "tmux was called")


if __name__ == "__main__":
    unittest.main()
