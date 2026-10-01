"""The reaper: a tmux-kind cousin stopped while its
runner is down still has a pane that may be running a turn. The supervisor
has it killed through `cousin-runner --reap-pane`, which holds the runner
lock while it kills and stays out of the way of a live runner."""
import subprocess
import threading
import unittest
from unittest import mock

from cousin_lib.runner import main, tmux_runner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class Pane:
    def __init__(self, alive=True):
        self._alive, self.kills = alive, 0

    def alive(self):
        return self._alive

    def kill(self):
        self.kills += 1
        self._alive = False


class TestReap(HermeticCase):
    def test_a_live_pane_is_killed_under_the_lock(self):
        home = temp_home(self)
        pane = Pane()
        self.assertEqual(tmux_runner.reap_pane(home, pane=pane), 0)
        self.assertEqual(pane.kills, 1)

    def test_no_pane_is_fine(self):
        self.assertEqual(tmux_runner.reap_pane(temp_home(self), pane=Pane(alive=False)), 0)

    def test_a_live_runner_keeps_its_pane(self):
        home = temp_home(self)
        held, release = threading.Event(), threading.Event()

        def hold():
            with main.hold_lock(home):
                held.set()
                release.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        pane = Pane()
        self.assertEqual(tmux_runner.reap_pane(home, pane=pane), main.LOCK_HELD_EXIT)
        self.assertEqual(pane.kills, 0)

    def test_the_cli_flag(self):
        home = temp_home(self)
        with mock.patch.object(tmux_runner, "pane_for", return_value=Pane()) as pf:
            self.assertEqual(main.runner_main(["--home", str(home), "--reap-pane"]), 0)
        pf.assert_called_once()


class TestSupervisorReaps(HermeticCase):
    def _sup(self, home, kind):
        from cousin_lib import supervisor
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n\n[agent]\nrunner = "%s"\n' % kind)
        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        sup.say = lambda *a: None
        return sup

    def test_a_tmux_cousin_is_reaped_and_an_sdk_one_is_not(self):
        home = temp_home(self)
        calls = []
        with mock.patch("cousin_lib.supervisor.subprocess.run",
                        side_effect=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")):
            self._sup(home, "tmux")._reap_pane(home)
            self._sup(home, "sdk")._reap_pane(home)
        self.assertEqual(len(calls), 1)
        self.assertIn("--reap-pane", calls[0])
        self.assertEqual(calls[0][calls[0].index("--home") + 1], str(home))


if __name__ == "__main__":
    unittest.main()
