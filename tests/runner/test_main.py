"""cousin-runner: the process a cousin lives in."""
import contextlib
import importlib.util
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


class _DeadWorker(FakeRunner):
    """A runner whose worker gives up at once, as SdkRunner's does on a
    fatal connect failure."""
    fatal = "cannot reach the model"

    def _loop(self):
        self.stream.append("error", {"error": self.fatal, "fatal": True})


class _StuckErrored(FakeRunner):
    """A runner whose worker lives on but never leaves `errored`."""

    def _loop(self):
        with self._lock:
            self.machine.to("errored", "stuck")
        self._stop.wait()


def _run(argv):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        rc = runner_main.runner_main(argv)
    return rc, stderr.getvalue()


def _wait_for(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _stream_says(home, text):
    for path in (home / "data" / "stream").glob("*.jsonl"):
        if text in path.read_text():
            return True
    return False


class TestRunnerSelection(HermeticCase):
    def test_a_cousin_with_no_runner_key_is_refused_not_given_an_sdk_session(self):
        home = temp_home(self)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 2)
        self.assertIn("[agent] runner", err)
        with self.assertRaises(runner_main.RunnerError):
            runner_main.runner_for(home)

    def test_the_command_line_still_overrides_a_missing_key(self):
        home = temp_home(self)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        self.assertEqual(type(runner_main.runner_for(home, kind="fake")).__name__, "FakeRunner")
        rc, _ = _run(["--home", str(home), "--runner", "fake", "--once"])
        self.assertEqual(rc, 0)


class TestLock(HermeticCase):
    def test_a_second_runner_on_the_same_home_is_refused(self):
        home = temp_home(self, runner="fake")
        lock = home / "run" / "runner.lock"
        holder = ("import fcntl, os, sys, time\n"
                  "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                  "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                  "print('locked', flush=True)\n"
                  "time.sleep(30)\n")
        with subprocess.Popen([sys.executable, "-c", holder, str(lock)],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            try:
                self.assertEqual(proc.stdout.readline(), b"locked\n")
                for argv in (["--home", str(home)], ["--home", str(home), "--once"]):
                    rc, err = _run(argv)
                    self.assertEqual(rc, 2)
                    self.assertIn(str(lock), err)
            finally:
                proc.kill()
                proc.wait(5)
        rc, _ = _run(["--home", str(home), "--once"])   # released: ours now
        self.assertEqual(rc, 0)


AUTH = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


@unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"), "needs the sdk extra")
class TestAuthLane(HermeticCase):
    def _capture(self, home):
        seen = {}

        class _Client:
            def __init__(self, options):
                seen["options_env"] = dict(options.env)
                seen["environ"] = {k: os.environ.get(k) for k in AUTH}

            async def connect(self, prompt=None):
                pass

            async def disconnect(self):
                pass

            async def interrupt(self):
                pass
        os.environ.update({"ANTHROPIC_API_KEY": "sk-inherited",
                           "ANTHROPIC_AUTH_TOKEN": "tok-inherited",
                           "ANTHROPIC_BASE_URL": "http://inherited.invalid"})
        with mock.patch("cousin_lib.runner.sdk._default_factory", _Client):
            rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 0, err)
        return seen

    def test_the_login_lane_carries_no_inherited_credential(self):
        home = temp_home(self, runner="sdk")
        seen = self._capture(home)
        self.assertEqual(seen["options_env"], {})
        self.assertEqual(seen["environ"], {k: None for k in AUTH})

    def test_the_key_lane_carries_only_its_file_key(self):
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir()
        (root / "keys").mkdir()
        (root / "keys" / "token").write_text("sk-from-file\n")
        _append_agent_key(home, "keys/token")
        seen = self._capture(home)
        self.assertEqual(seen["options_env"], {"ANTHROPIC_API_KEY": "sk-from-file"})
        self.assertEqual(seen["environ"], {k: None for k in AUTH})


class TestWorkerDeath(HermeticCase):
    def test_once_exits_3_when_the_worker_is_gone(self):
        home = temp_home(self, runner="fake")
        Inbox(home).put(Item("operator:priya", "chat", "a", sender="Priya"))
        with mock.patch.object(runner_main, "runner_for",
                               lambda h, kind=None: _DeadWorker(h)):
            rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 3)
        self.assertIn("cannot reach the model", err)

    def test_the_daemon_exits_3_when_the_worker_is_gone(self):
        home = temp_home(self, runner="fake")
        with mock.patch.object(runner_main, "runner_for",
                               lambda h, kind=None: _DeadWorker(h)):
            rc, err = _run(["--home", str(home)])
        self.assertEqual(rc, 3)

    def test_once_gives_up_on_a_runner_stuck_in_errored(self):
        home = temp_home(self, runner="fake")
        Inbox(home).put(Item("operator:priya", "chat", "a", sender="Priya"))
        with mock.patch.object(runner_main, "runner_for",
                               lambda h, kind=None: _StuckErrored(h)), \
                mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.3):
            t = time.monotonic()
            rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 3)
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertIn("errored", err)


class TestSignalHandlersInstalledInTheTry(HermeticCase):
    def test_a_failing_second_install_still_restores_the_first(self):
        home = temp_home(self, runner="fake")
        previous = signal.getsignal(signal.SIGTERM)
        real = signal.signal

        def flaky(signum, handler):
            if signum == signal.SIGINT:
                raise ValueError("no SIGINT here")
            return real(signum, handler)
        with mock.patch.object(runner_main.signal, "signal", side_effect=flaky):
            with self.assertRaises(ValueError):
                runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


class TestSigterm(HermeticCase):
    def _spawn(self, argv):
        return subprocess.Popen(argv, cwd=os.getcwd(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def test_sigterm_stops_the_process_cleanly(self):
        home = temp_home(self, runner="fake")
        from cousin_lib.runner import wake
        with self._spawn([sys.executable, "-m", "cousin_lib.runner.main",
                          "--home", str(home)]) as proc:
            try:
                self.assertTrue(_wait_for(wake.socket_path(home).exists))
                self.assertIsNone(proc.poll())
                row = Inbox(home).put(Item("operator:priya", "chat", "x", sender="Priya"))
                wake.poke(home)
                self.assertTrue(_wait_for(lambda: Inbox(home).get(row)["state"] == "done"))
                proc.send_signal(signal.SIGTERM)
                rc = proc.wait(10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(5)
        self.assertEqual(rc, 0)
        self.assertEqual(Inbox(home).unfinished(), 0)

    def test_sigterm_during_once_stops_it(self):
        home = temp_home(self, runner="fake")
        row = Inbox(home).put(Item("operator:priya", "chat", "slow", sender="Priya"))
        slow = ("import sys\n"
                "from cousin_lib.runner import main\n"
                "from cousin_lib.runner.fake import FakeRunner\n"
                "main.runner_for = lambda home, kind=None: FakeRunner(home, turn_seconds=60)\n"
                "sys.exit(main.runner_main(['--home', sys.argv[1], '--once']))\n")
        with self._spawn([sys.executable, "-c", slow, str(home)]) as proc:
            try:
                self.assertTrue(_wait_for(lambda: _stream_says(home, '"to": "running"')))
                t = time.monotonic()
                proc.send_signal(signal.SIGTERM)
                rc = proc.wait(10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(5)
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertEqual(Inbox(home).get(row)["state"], "done")



class TestPolicyAtStart(HermeticCase):
    def test_a_malformed_policy_is_a_config_error(self):
        home = temp_home(self, runner="fake")
        (home / "policy.toml").write_text('deny_tools = "x"\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 2)
        self.assertIn("deny_tools", err.getvalue())

    def test_the_policy_is_described_in_the_stream_at_start(self):
        home = temp_home(self, runner="fake")
        Inbox(home).put(Item("operator:priya", "chat", "a", sender="Priya"))
        rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 0)
        import json
        events = []
        for p in (home / "data" / "stream").glob("*.jsonl"):
            events += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        policy_events = [e for e in events
                         if e["kind"] == "policy" and "no policy.toml" in json.dumps(e)]
        self.assertTrue(policy_events)

    def test_both_runners_are_handed_the_policy(self):
        home = temp_home(self, runner="fake")
        (home / "policy.toml").write_text('deny_tools = ["WebFetch"]\n')
        r = runner_main.runner_for(home)
        self.assertEqual(r.policy.deny_tools, ("WebFetch",))

    def test_the_runner_never_writes_harness_settings(self):
        from cousin_lib import harness_settings
        home = temp_home(self, runner="fake")
        with mock.patch.object(harness_settings, "apply_project_settings") as apply:
            rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 0)
        apply.assert_not_called()


class TestIsRunning(HermeticCase):
    def test_is_running_is_false_with_no_lock_file(self):
        home = temp_home(self, runner="fake")
        self.assertFalse(runner_main.is_running(home))

    def test_is_running_is_true_while_a_runner_holds_the_lock(self):
        home = temp_home(self, runner="fake")
        lock = home / "run" / "runner.lock"
        holder = ("import fcntl, os, sys, time\n"
                  "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                  "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                  "print('locked', flush=True)\n"
                  "time.sleep(30)\n")
        with subprocess.Popen([sys.executable, "-c", holder, str(lock)],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            try:
                self.assertEqual(proc.stdout.readline(), b"locked\n")
                self.assertTrue(runner_main.is_running(home))
            finally:
                proc.kill()
                proc.wait(5)
        self.assertFalse(runner_main.is_running(home))

    def test_is_running_releases_the_lock_it_takes(self):
        home = temp_home(self, runner="fake")
        # a probe that leaked its own lock would make the real runner
        # below refuse, exactly like a second runner would
        self.assertFalse(runner_main.is_running(home))
        rc, _ = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 0)


class TestHoldLock(HermeticCase):
    def test_hold_lock_is_visible_to_is_running(self):
        home = temp_home(self, runner="fake")
        with runner_main.hold_lock(home):
            self.assertTrue(runner_main.is_running(home))
        self.assertFalse(runner_main.is_running(home))

    def test_a_second_runner_is_still_refused_while_hold_lock_is_held(self):
        home = temp_home(self, runner="fake")
        with runner_main.hold_lock(home):
            rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 2)
        self.assertIn("runner.lock", err)


if __name__ == "__main__":
    unittest.main()
