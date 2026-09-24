"""The runner lane's start and stop go through cousin-supervisor (phase 6
task 2, R10): `spawn.start_cousin`/`stop_cousin` on a cousin whose
`[agent] runner` is sdk or fake ask the supervisor over its socket and
never touch tmux; the tmux lane is unchanged. A stub supervisor answers
the protocol; one test runs the real one over a `fake` runner. Invented
cast only; every wait has a deadline."""
import contextlib
import io
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
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

    def test_no_supervisor_is_not_running_on_both_halves_and_still_holds(self):
        # O9: the stop is the operator's decision whether or not a
        # supervisor happens to be up: the hold is written here, so the
        # next supervisor does not start the cousin until `start`
        home = runner_home(self.root, "wren")
        self.assertFalse(supervisor.is_held(home))
        out = spawn.stop_cousin(home, tmux_bin=str(self.tmux), root=self.root, by="Testa")
        self.assertEqual(out, {"runner": "not running", "supervisor": "not running",
                               "held": True})
        self.assertEqual(self.tmux_calls(), "")
        self.assertTrue(supervisor.is_held(home))
        self.assertTrue(supervisor.held_path(home).read_text().rstrip().endswith(" Testa"))
        self.assertNotIn(home, supervisor.runner_cousins(self.root))   # a new supervisor skips it

    def test_a_hand_started_runner_with_no_supervisor_reports_running(self):
        # item 3: with no supervisor, a runner started by hand (bypassing
        # cousin-supervisor) still holds its lock; the stop must check
        # delivery.is_alive before claiming "not running" - a truthful
        # "running" (nothing here can signal it, there is no supervisor),
        # still held so the next supervisor leaves it down until `start`.
        home = runner_home(self.root, "wren")
        with mock.patch("cousin_lib.delivery.is_alive", lambda home, **kw: True):
            out = spawn.stop_cousin(home, tmux_bin=str(self.tmux), root=self.root, by="Testa")
        self.assertEqual(out, {"runner": "running", "supervisor": "not running",
                               "held": True})
        self.assertTrue(supervisor.is_held(home))

    def test_a_hold_that_cannot_be_written_says_so(self):
        home = runner_home(self.root, "wren")
        with mock.patch("cousin_lib.supervisor.hold", side_effect=PermissionError("read-only")):
            out = spawn.stop_cousin(home, root=self.root)
        self.assertEqual(out, {"runner": "not running", "supervisor": "not running",
                               "held": False, "error": "cannot hold: read-only"})

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


# ---- phase 6 task 2, second half: a new cousin is a runner cousin where the install says so

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_TOML = ('[cousin]\nslug = "wren"\nname = "Wren"\nrole = "example cousin"\n\n'
             '[chat]\nport = 8100\ntmux_session = "wren"\n')
ACCOUNTS = ('[accounts.metered]\nkind = "anthropic-key"\n\n'
            '[accounts.fleet]\nkind = "claude-login"\n')


class _CreateCase(_Case):
    def setUp(self):
        super().setUp()
        (self.root / "templates").mkdir()
        shutil.copy(_REPO_ROOT / "templates" / "cousin-CLAUDE.template.md",
                    self.root / "templates" / "cousin-CLAUDE.template.md")
        self.home = self.root / "cousins" / "wren"

    def accounts(self):
        (self.root / "config" / "accounts.toml").write_text(ACCOUNTS)

    def create(self, **kw):
        args = dict(slug="wren", name="Wren", role="example cousin",
                    voice="Plain and helpful.", port=8100)
        args.update(kw)
        return spawn.create_cousin(self.root, **args)

    def toml(self):
        return (self.home / "cousin.toml").read_text()

    def refused(self, **kw):
        with self.assertRaises(spawn.SpawnError) as caught:
            self.create(**kw)
        self.assertFalse(self.home.exists(), "a refused create wrote %s" % self.home)
        return str(caught.exception)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn.spawn_main(list(argv) + ["--root", str(self.root)])
        return rc, out.getvalue(), err.getvalue()


class TestCreateOnTheRunnerLane(_CreateCase):
    def test_no_env_and_no_argument_is_the_tmux_cousin_unchanged(self):
        """guard: the cousin.toml spawn wrote before this task, byte for byte."""
        self.create()
        self.assertEqual(self.toml(), BASE_TOML)

    def test_an_empty_env_is_the_tmux_lane(self):
        os.environ["COUSIN_DEFAULT_RUNNER"] = ""
        os.environ["COUSIN_DEFAULT_ACCOUNT"] = ""
        self.create()
        self.assertEqual(self.toml(), BASE_TOML)

    def test_the_env_default_runner_is_written(self):
        os.environ["COUSIN_DEFAULT_RUNNER"] = "sdk"
        self.create()
        self.assertEqual(self.toml(), BASE_TOML + '\n[agent]\nrunner = "sdk"\n')

    def test_the_env_default_account_goes_with_the_runner(self):
        self.accounts()
        os.environ["COUSIN_DEFAULT_RUNNER"] = "sdk"
        os.environ["COUSIN_DEFAULT_ACCOUNT"] = "metered"
        self.create()
        self.assertEqual(self.toml(), BASE_TOML
                         + '\n[agent]\nrunner = "sdk"\naccount = "metered"\n')

    def test_an_env_account_without_a_runner_writes_nothing(self):
        self.accounts()
        os.environ["COUSIN_DEFAULT_ACCOUNT"] = "metered"
        self.create()
        self.assertEqual(self.toml(), BASE_TOML)

    def test_explicit_arguments_beat_the_env(self):
        self.accounts()
        os.environ["COUSIN_DEFAULT_RUNNER"] = "sdk"
        os.environ["COUSIN_DEFAULT_ACCOUNT"] = "metered"
        self.create(runner="fake", account="fleet")
        data = tomllib.loads(self.toml())
        self.assertEqual(data["agent"], {"runner": "fake", "account": "fleet"})

    def test_an_invalid_runner_is_refused_before_anything_is_written(self):
        self.assertIn("runner must be one of sdk, fake", self.refused(runner="tmux"))
        os.environ["COUSIN_DEFAULT_RUNNER"] = "docker"
        self.assertIn("COUSIN_DEFAULT_RUNNER", self.refused())

    def test_an_unknown_account_is_refused_before_anything_is_written(self):
        self.assertIn("not in config/accounts.toml",
                      self.refused(runner="sdk", account="metered"))   # no accounts.toml
        self.accounts()
        self.assertIn("not in config/accounts.toml",
                      self.refused(runner="sdk", account="nobody"))
        os.environ["COUSIN_DEFAULT_RUNNER"] = "sdk"
        os.environ["COUSIN_DEFAULT_ACCOUNT"] = "nobody"
        self.assertIn("COUSIN_DEFAULT_ACCOUNT", self.refused())

    def test_an_account_without_a_runner_is_refused(self):
        self.accounts()
        self.assertIn("needs a runner", self.refused(account="metered"))

    def test_the_created_cousin_is_the_supervisors(self):
        self.create(runner="fake")
        self.assertTrue(spawn.runner_lane(self.home))
        self.assertEqual([c.slug for c in supervisor.runner_cousins(self.root)], ["wren"])

    def test_a_runner_that_is_not_a_runner_kind_is_not_the_runner_lane(self):
        """#100 review: a second runner_lane returned the raw string, so
        `runner = "tmux"` read as the runner lane everywhere."""
        self.create()
        text = self.toml()
        for runner, lane in (("tmux", False), ("", False), ("sdk", True),
                             ("fake", True), ("opencode", True)):
            (self.home / "cousin.toml").write_text(
                text + '\n[agent]\nrunner = "%s"\n' % runner)
            self.assertIs(spawn.runner_lane(self.home), lane, runner)


class TestCreateARunnerCousinsModel(_CreateCase):
    """Audit defect 1, spawn half: a runner reads [agent] model and effort
    only, so a runner cousin's go there, checked by its lane
    (agent_settings.check_new) before anything is written."""

    def test_a_runner_cousins_model_and_effort_go_to_agent_not_runtime(self):
        self.accounts()
        self.create(runner="sdk", account="fleet", model="claude-x", effort="high")
        data = tomllib.loads(self.toml())
        self.assertNotIn("runtime", data)
        self.assertEqual(data["agent"], {"runner": "sdk", "account": "fleet",
                                         "model": "claude-x", "effort": "high"})

    def test_a_tmux_cousins_model_stays_in_runtime(self):
        self.create(model="claude-x", effort="low")
        data = tomllib.loads(self.toml())
        self.assertEqual(data["runtime"], {"model": "claude-x", "effort": "low"})
        self.assertNotIn("agent", data)

    def test_the_lane_refuses_what_it_does_not_read(self):
        self.assertIn("model", self.refused(runner="fake", model="claude-x"))
        self.assertIn("effort", self.refused(runner="opencode", effort="high"))

    def test_the_account_must_run_on_the_lane(self):
        (self.root / "config" / "accounts.toml").write_text(
            ACCOUNTS + '\n[accounts.oc]\nkind = "opencode"\nproviders = ["openai"]\n')
        self.assertIn("kind opencode", self.refused(runner="sdk", account="oc"))
        self.assertIn("P9-1", self.refused(runner="opencode", account="oc",
                                           model="openai/claude-x"))
        self.create(runner="opencode", account="oc", model="openai/gpt-5")
        self.assertEqual(tomllib.loads(self.toml())["agent"]["model"], "openai/gpt-5")


class TestSpawnCliOnTheRunnerLane(_CreateCase):
    ARGS = ("wren", "--name", "Wren", "--role", "example cousin",
            "--voice", "Plain and helpful.", "--port", "8100")

    def test_runner_and_account_flags(self):
        self.accounts()
        rc, out, err = self.cli(*self.ARGS, "--runner", "fake", "--account", "metered")
        self.assertEqual(rc, 0, err)
        self.assertEqual(tomllib.loads(self.toml())["agent"],
                         {"runner": "fake", "account": "metered"})

    def test_an_unknown_account_exits_2_with_nothing_written(self):
        rc, out, err = self.cli(*self.ARGS, "--runner", "fake", "--account", "nobody")
        self.assertEqual(rc, 2)
        self.assertIn("nobody", err)
        self.assertFalse(self.home.exists())

    def test_start_on_the_runner_lane_asks_the_supervisor_not_tmux(self):
        # no config/agent-cmd and no tmux: neither is the runner lane's
        stub = self.stub()
        os.environ["PATH"] = str(self.dir / "empty-bin")
        rc, out, err = self.cli(*self.ARGS, "--runner", "fake", "--start")
        self.assertEqual(rc, 0, err)
        self.assertEqual(stub.ops(), [("start", "wren")])
        self.assertIn("started wren", out)

    def test_start_of_an_existing_runner_cousin_asks_the_supervisor(self):
        self.create(runner="fake")
        stub = self.stub()
        rc, out, err = self.cli("wren", "--start")
        self.assertEqual(rc, 0, err)
        self.assertEqual(stub.ops(), [("start", "wren")])

    def test_start_of_an_existing_runner_never_already_running_while_held(self):
        # item 2, CLI half: delivery.is_alive can still read True while
        # the runner finishes its stop (up to ~35s); supervisor.is_held
        # is true throughout, and must skip the alive short-circuit so a
        # Start in that window reaches the supervisor and reports its
        # true answer, not a stale "already running".
        from cousin_lib import supervisor
        self.create(runner="fake")
        stub = self.stub(start={"ok": False, "name": "runner:wren",
                                "error": "runner:wren is still stopping;"
                                         " start it once it is down"})
        supervisor.hold(self.home, "Testa")
        with mock.patch("cousin_lib.delivery.is_alive", lambda home, **kw: True):
            rc, out, err = self.cli("wren", "--start")
        self.assertEqual(rc, 1)
        self.assertNotIn("already running", out)
        self.assertEqual(stub.ops(), [("start", "wren")])
        self.assertIn("still stopping", err)

    def test_start_with_no_supervisor_keeps_the_home_and_says_how(self):
        rc, out, err = self.cli(*self.ARGS, "--runner", "fake", "--start")
        self.assertEqual(rc, 1)
        self.assertIn("cousin-supervisor run", err)
        self.assertTrue((self.home / "cousin.toml").is_file())
