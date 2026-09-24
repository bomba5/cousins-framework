"""The runner lane's start and stop go through cousin-supervisor (phase 6
task 2, R10): `spawn.start_cousin`/`stop_cousin` on a cousin whose
`[agent] runner` is sdk or fake ask the supervisor over its socket and
never touch tmux; the tmux lane is unchanged. A stub supervisor answers
the protocol; one test runs the real one over a `fake` runner. Invented
cast only; every wait has a deadline."""
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import time
from unittest import mock

from cousin_lib import spawn, supervisor
from cousin_lib.runner.main import is_running
from tests._hermetic import HermeticCase
from tests._stub_supervisor import StubSupervisor, runner_home
from tests.server.test_injection import _FAKE_TMUX


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


class _Case(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.root = self.dir / "root"
        (self.root / "config").mkdir(parents=True)
        self.tmux = self.dir / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.tmux_log = self.dir / "tmux.log"
        os.environ["FAKE_TMUX_LOG"] = str(self.tmux_log)
        os.environ["FAKE_TMUX_PANE"] = str(self.dir / "pane")

    def stub(self, **answers):
        stub = StubSupervisor(self.root, answers).start()
        self.addCleanup(stub.close)
        return stub

    def tmux_calls(self):
        return self.tmux_log.read_text() if self.tmux_log.exists() else ""


class TestRunnerLaneStart(_Case):
    def test_start_asks_the_supervisor_and_never_tmux(self):
        home = runner_home(self.root, "wren")
        stub = self.stub()
        out = spawn.start_cousin(home, agent_cmd=None, tmux_bin=str(self.tmux),
                                 root=self.root, start_chat_server=self.fail)
        self.assertIsNone(out)                  # what the tmux path returns
        self.assertEqual(stub.ops(), [("start", "wren")])
        self.assertEqual(self.tmux_calls(), "")

    def test_the_root_comes_from_the_home_when_not_given(self):
        home = runner_home(self.root, "wren", runner="sdk")
        stub = self.stub()
        spawn.start_cousin(home, agent_cmd=None, tmux_bin=str(self.tmux))
        self.assertEqual(stub.ops(), [("start", "wren")])

    def test_no_supervisor_is_a_spawn_error_that_says_how(self):
        home = runner_home(self.root, "wren")
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.start_cousin(home, agent_cmd=None, tmux_bin=str(self.tmux), root=self.root)
        self.assertEqual(str(caught.exception),
                         "no cousin-supervisor is running for %s: start it with"
                         " `cousin-supervisor run`" % self.root)
        self.assertEqual(self.tmux_calls(), "")

    def test_a_refused_start_is_a_spawn_error_with_its_reason(self):
        home = runner_home(self.root, "wren")
        self.stub(start={"ok": False, "name": "runner:wren",
                         "error": "runner:wren is still stopping; start it once it is down"})
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.start_cousin(home, agent_cmd=None, root=self.root)
        self.assertIn("runner:wren is still stopping", str(caught.exception))


class TestRunnerLaneStop(_Case):
    def test_stop_asks_the_supervisor_with_the_runner_timeout(self):
        home = runner_home(self.root, "wren")
        stub = self.stub()
        with mock.patch("cousin_lib.supervisor.request", wraps=supervisor.request) as req:
            out = spawn.stop_cousin(home, tmux_bin=str(self.tmux))
        self.assertEqual(out, {"runner": "stopped", "supervisor": "running"})
        self.assertEqual(stub.ops(), [("stop", "wren")])
        self.assertEqual(req.call_args.kwargs["timeout"], 60)
        self.assertEqual((stub.requests[0]["wait"], stub.requests[0]["by"]),
                         (True, "spawn.stop_cousin"))       # CLI callers want the result
        self.assertEqual(self.tmux_calls(), "")

    def test_stop_without_wait_is_stopping(self):
        home = runner_home(self.root, "wren")
        stub = self.stub()
        out = spawn.stop_cousin(home, root=self.root, wait=False, by="Testa")
        self.assertEqual(out, {"runner": "stopping", "supervisor": "running"})
        self.assertEqual((stub.requests[0]["wait"], stub.requests[0]["by"]), (False, "Testa"))

    def test_the_runner_kinds_are_delivery_s(self):
        from cousin_lib import delivery
        self.assertIs(spawn.RUNNER_KINDS, delivery.RUNNER_KINDS)     # M6

    def test_no_supervisor_is_not_running_on_both_halves(self):
        home = runner_home(self.root, "wren")
        out = spawn.stop_cousin(home, tmux_bin=str(self.tmux), root=self.root)
        self.assertEqual(out, {"runner": "not running", "supervisor": "not running"})
        self.assertEqual(self.tmux_calls(), "")

    def test_a_runner_the_supervisor_does_not_hold_is_not_running(self):
        home = runner_home(self.root, "wren")
        self.stub(stop={"ok": False, "error": "no child named wren"})
        out = spawn.stop_cousin(home, root=self.root)
        self.assertEqual(out, {"runner": "not running", "supervisor": "running"})

    def test_a_refused_stop_carries_the_reason(self):
        home = runner_home(self.root, "wren")
        self.stub(stop={"ok": False, "error": "the supervisor is stopping"})
        out = spawn.stop_cousin(home, root=self.root)
        self.assertEqual(out, {"runner": "unknown", "supervisor": "running",
                               "error": "the supervisor is stopping"})


class TestTmuxLaneUnchanged(_Case):
    """guard: a tmux cousin never reaches the supervisor, even with one up."""

    def test_start_and_stop_stay_on_tmux(self):
        home = self.root / "cousins" / "sam"
        (home / "data").mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "sam"\nname = "Sam"\n\n[chat]\ntmux_session = "sam"\n')
        stub = self.stub()
        started = []
        spawn.start_cousin(home, agent_cmd="my-agent", tmux_bin=str(self.tmux),
                           root=self.root, start_chat_server=started.append)
        self.assertIn("new-session", self.tmux_calls())
        self.assertEqual(started, [home])
        out = spawn.stop_cousin(home, tmux_bin=str(self.tmux), port_pid=lambda p: None)
        self.assertEqual(set(out), {"tmux", "chat_server"})
        self.assertEqual(stub.requests, [])


class TestRealSupervisor(_Case):
    """start_cousin and stop_cousin against a real `cousin-supervisor run`
    over a `fake` runner cousin that does not auto-start."""

    def test_start_then_stop_a_fake_runner(self):
        home = runner_home(self.root, "toki", extra="auto_start = false\n")
        log = open(self.dir / "supervisor.log", "w")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-m", "cousin_lib.supervisor", "run",
                                 "--root", str(self.root), "--no-console", "--no-loops"],
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

        def _cleanup():
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(40)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(5)
        self.addCleanup(_cleanup)
        self.assertTrue(_wait_for(lambda: supervisor.snapshot(self.root)),
                        (self.dir / "supervisor.log").read_text())
        self.assertFalse(is_running(home))
        spawn.start_cousin(home, agent_cmd=None)
        self.assertTrue(_wait_for(lambda: is_running(home)),
                        (self.dir / "supervisor.log").read_text())
        out = spawn.stop_cousin(home)
        self.assertEqual(out, {"runner": "stopped", "supervisor": "running"})
        self.assertFalse(is_running(home))
        # R4': the stop went through the socket, so it holds the cousin down
        # past a supervisor restart; the next start lifts it
        held = home / "run" / "held"
        self.assertTrue(held.read_text().endswith(" spawn.stop_cousin\n"))
        self.assertTrue(supervisor.is_held(home))
        spawn.start_cousin(home, agent_cmd=None)
        self.assertFalse(held.exists())
        self.assertTrue(_wait_for(lambda: is_running(home)),
                        (self.dir / "supervisor.log").read_text())
        spawn.stop_cousin(home, wait=False)
        self.assertTrue(held.exists())
