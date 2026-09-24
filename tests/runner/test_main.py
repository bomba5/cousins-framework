"""cousin-runner: the process a cousin lives in."""
import contextlib
import importlib.util
import io
import json
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
        # exit 5 (busy), not 2 (configuration): a supervisor retries a busy
        # runner after its backoff instead of leaving it `failing`
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
                    self.assertEqual(rc, runner_main.LOCK_HELD_EXIT)
                    self.assertEqual(rc, 5)
                    self.assertIn(str(lock), err)
            finally:
                proc.kill()
                proc.wait(5)
        rc, _ = _run(["--home", str(home), "--once"])   # released: ours now
        self.assertEqual(rc, 0)

    def test_a_lock_that_cannot_be_opened_is_still_configuration(self):
        # only a HELD lock is busy; a lock path that cannot be opened is exit 2
        home = temp_home(self, runner="fake")
        (home / "run" / "runner.lock").mkdir(parents=True)
        rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot open the runner lock", err)


AUTH = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


@unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"), "needs the sdk extra")
class TestAuthLane(HermeticCase):
    def _capture(self, home):
        seen = {}

        class _Client:
            def __init__(self, options):
                seen["options_env"] = dict(options.env)
                seen["environ"] = {k: os.environ.get(k) for k in AUTH}
                seen["model"], seen["effort"] = options.model, options.effort

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
        self.assertEqual(seen["options_env"], {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.assertEqual(seen["environ"], {k: None for k in AUTH})

    def test_no_auth_or_provider_variable_from_the_shell_reaches_the_child(self):
        # the CLI's own list of auth variables and the provider switches
        shell = {"ANTHROPIC_API_KEY": "sk-shell", "ANTHROPIC_AUTH_TOKEN": "tok-shell",
                 "ANTHROPIC_BASE_URL": "http://shell.invalid",
                 "CLAUDE_CODE_OAUTH_TOKEN": "oauth-shell", "CLAUDE_CONFIG_DIR": "/shell/dir",
                 "AWS_BEARER_TOKEN_BEDROCK": "bedrock-shell",
                 "ANTHROPIC_FOUNDRY_API_KEY": "foundry-key-shell",
                 "ANTHROPIC_FOUNDRY_AUTH_TOKEN": "foundry-tok-shell",
                 "ANTHROPIC_AWS_API_KEY": "aws-key-shell",
                 "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
                 "CLAUDE_CODE_USE_FOUNDRY": "1"}
        seen = {}

        class _Client:
            def __init__(self, options):
                # the SDK starts the CLI with {**os.environ, **options.env}
                child = {**os.environ, **options.env}
                seen["child"] = {k: child.get(k) for k in shell}

            async def connect(self, prompt=None):
                pass

            async def disconnect(self):
                pass

            async def interrupt(self):
                pass
        os.environ.update(shell)
        home = temp_home(self, runner="sdk")
        with mock.patch("cousin_lib.runner.sdk._default_factory", _Client):
            rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen["child"], {k: None for k in shell})

    def test_the_key_lane_carries_only_its_file_key(self):
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir()
        (root / "keys").mkdir(); os.chmod(root / "keys", 0o700)
        (root / "keys" / "token").write_text("sk-from-file\n"); os.chmod(root / "keys" / "token", 0o600)
        _append_agent_key(home, "keys/token")
        seen = self._capture(home)
        self.assertEqual(seen["options_env"], {
            "ANTHROPIC_API_KEY": "sk-from-file",
            "CLAUDE_CONFIG_DIR": str(root / "data" / "accounts" / "wren"),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.assertEqual(seen["environ"], {k: None for k in AUTH})

    def test_agent_model_effort_and_account_reach_the_options(self):
        """#96: what cousin-migrate carries from [runtime] into [agent]
        reaches the CLI: the model, the effort (ClaudeAgentOptions.effort,
        the CLI's --effort) and the key account's key."""
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir()
        (root / "config" / "accounts.toml").write_text(
            '[accounts.wren-key]\nkind = "anthropic-key"\n')
        secrets = root / ".secrets" / "accounts"
        secrets.mkdir(parents=True); os.chmod(root / ".secrets", 0o700); os.chmod(secrets, 0o700)
        (secrets / "wren-key").write_text("sk-ant-fixture-wren\n"); os.chmod(secrets / "wren-key", 0o600)
        (home / "cousin.toml").write_text((home / "cousin.toml").read_text()
                                          + 'model = "opus"\neffort = "xhigh"\naccount = "wren-key"\n')
        seen = self._capture(home)
        self.assertEqual((seen["model"], seen["effort"]), ("opus", "xhigh"))
        self.assertEqual(seen["options_env"]["ANTHROPIC_API_KEY"], "sk-ant-fixture-wren")

    def test_no_effort_leaves_the_cli_its_own(self):
        home = temp_home(self, runner="sdk")
        seen = self._capture(home)
        self.assertEqual((seen["model"], seen["effort"]), (None, None))

    def test_an_effort_the_cli_would_refuse_is_a_configuration_error(self):
        home = temp_home(self, runner="sdk")
        (home / "cousin.toml").write_text((home / "cousin.toml").read_text() + 'effort = "ludicrous"\n')
        rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 2)
        self.assertIn("effort", err)


class TestAccountBeforeTheLock(HermeticCase):
    def test_a_secret_open_to_others_is_exit_2_before_the_lock(self):
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir()
        (root / "keys").mkdir(); os.chmod(root / "keys", 0o700)
        (root / "keys" / "token").write_text("sk-from-file\n"); os.chmod(root / "keys" / "token", 0o644)
        _append_agent_key(home, "keys/token")
        # a lock taken would raise at once (runner_main catches only RunnerError),
        # so a regression fails here instead of serving forever
        with mock.patch.object(runner_main, "hold_lock",
                               side_effect=AssertionError("lock taken before the account check")) as lock:
            rc, err = _run(["--home", str(home)])
        self.assertEqual(rc, 2)
        lock.assert_not_called()                          # refused before the lock is taken
        self.assertIn("chmod 600", err); self.assertNotIn("sk-from-file", err)


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


class TestEnvironmentAtStart(HermeticCase):
    """The model's own commands and the in-process tools read COUSIN_HOME
    and FRAMEWORK_ROOT; a runner started without them exports both."""

    def _cwd(self, path):
        old = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, old)

    def _tool_calls_succeed(self, ctx):
        from cousin_lib.runner import tools
        text, err = tools.call(ctx, "schedule", {"command": "add", "when": "in 2h",
                                                 "prompt": "water the plants"})
        self.assertFalse(err, text)
        text, err = tools.call(ctx, "job", {"command": "start", "kind": "subagent",
                                            "title": "env check"})
        self.assertFalse(err, text)

    def test_runner_main_exports_the_home_and_the_root_from_a_relative_home(self):
        from pathlib import Path
        from cousin_lib.runner import tools
        from cousin_lib.runner.policy import Policy
        home = temp_home(self, runner="fake")
        root = home.parent.parent
        (root / "config").mkdir()
        self.assertNotIn("COUSIN_HOME", os.environ)
        self.assertNotIn("FRAMEWORK_ROOT", os.environ)
        inbox_id = Inbox(home).put(Item("operator:priya", "chat", "a", sender="Priya"))
        self._cwd(root)
        rc, err = _run(["--home", "cousins/wren", "--once"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(Inbox(home).get(inbox_id)["state"], "done")
        self.assertEqual(os.environ["COUSIN_HOME"], str(home))
        self.assertEqual(os.environ["FRAMEWORK_ROOT"], str(root))
        self._tool_calls_succeed(tools.ToolContext(
            home=Path(os.environ["COUSIN_HOME"]), slug="wren", name="Wren",
            root=Path(os.environ["FRAMEWORK_ROOT"]), turn=None, policy=Policy()))

    def test_a_home_with_no_config_dir_above_exports_its_grandparent(self):
        home = temp_home(self, runner="fake")
        rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(os.environ["FRAMEWORK_ROOT"], str(home.parent.parent))

    def test_an_sdk_runner_built_directly_exports_both_when_unset(self):
        from cousin_lib.runner.sdk import SdkRunner
        home = temp_home(self, runner="sdk")
        (home.parent.parent / "config").mkdir()
        r = SdkRunner(home, client_factory=lambda options: None)
        self.assertEqual(os.environ["COUSIN_HOME"], str(home))
        self.assertEqual(os.environ["FRAMEWORK_ROOT"], str(home.parent.parent))
        self._tool_calls_succeed(r.tool_context)

    def test_an_sdk_runner_leaves_variables_that_are_already_set(self):
        from cousin_lib.runner.sdk import SdkRunner
        home = temp_home(self, runner="sdk")
        os.environ["FRAMEWORK_ROOT"] = "/elsewhere/root"
        os.environ["COUSIN_HOME"] = "/elsewhere/root/cousins/wren"
        SdkRunner(home, client_factory=lambda options: None)
        self.assertEqual(os.environ["FRAMEWORK_ROOT"], "/elsewhere/root")
        self.assertEqual(os.environ["COUSIN_HOME"], "/elsewhere/root/cousins/wren")


class TestRegistryAtStart(HermeticCase):
    """A registry the runner cannot serve is a configuration problem,
    rc 2 at start, not a worker that dies on its first connect (rc 3)."""

    def _refuse(self, home, needle):
        from cousin_lib.runner.sdk import SdkRunner
        with mock.patch.object(SdkRunner, "options",
                               side_effect=AssertionError("options() reached")):
            rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 2, err)
        self.assertIn(needle, err)
        self.assertNotIn("options() reached", err)

    def test_a_registry_tool_with_no_handler_is_rc_2_naming_it(self):
        home = temp_home(self, runner="sdk")
        (home / "mcp-registry.toml").write_text(
            '[tools.weather]\ncommand = "cousin-weather"\n'
            'description = "Tomorrow\'s forecast."\n\n'
            '[tools.weather.commands.today]\nargv = ["today"]\n')
        self._refuse(home, "weather.today")

    def test_a_malformed_registry_is_rc_2(self):
        home = temp_home(self, runner="sdk")
        (home / "mcp-registry.toml").write_text("this is not [ toml\n")
        self._refuse(home, "mcp-registry.toml")


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
        self.assertEqual(rc, 5)
        self.assertIn("runner.lock", err)


class TestHoldLockRetry(HermeticCase):
    """#79 (review N4): is_running probes by taking the lock for
    microseconds; hold_lock retries LOCK_EX|LOCK_NB for LOCK_TAKE_S before
    LockHeld, so a runner starting inside a probe is never refused."""

    _HOLDER = ("import fcntl, os, sys, time\n"
               "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
               "fcntl.flock(fd, fcntl.LOCK_EX)\n"
               "print('locked', flush=True)\n"
               "time.sleep(float(sys.argv[2]))\n")

    def _holder(self, lock, hold_for):
        proc = subprocess.Popen([sys.executable, "-c", self._HOLDER, str(lock), str(hold_for)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        self.assertEqual(proc.stdout.readline(), b"locked\n")
        return proc

    def test_the_retry_window_is_about_a_second(self):
        self.assertEqual(runner_main.LOCK_TAKE_S, 1.0)

    def test_a_holder_that_lets_go_inside_the_window_is_waited_for(self):
        home = temp_home(self, runner="fake")
        lock = home / "run" / "runner.lock"
        self._holder(lock, 0.3)
        started = time.monotonic()
        with runner_main.hold_lock(home):
            waited = time.monotonic() - started
            self.assertTrue(runner_main.is_running(home))
        self.assertLess(waited, runner_main.LOCK_TAKE_S + 0.5)

    def test_a_holder_that_keeps_it_is_lock_held_after_the_window(self):
        home = temp_home(self, runner="fake")
        lock = home / "run" / "runner.lock"
        self._holder(lock, 30)
        started = time.monotonic()
        with self.assertRaises(runner_main.LockHeld) as caught:
            with runner_main.hold_lock(home):
                self.fail("took a lock another process holds")
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, runner_main.LOCK_TAKE_S * 0.9)
        self.assertLess(waited, runner_main.LOCK_TAKE_S + 2.0)
        self.assertIn(str(lock), str(caught.exception))


class TestStopTimeout(HermeticCase):
    def test_serve_stops_the_runner_with_the_one_constant(self):
        # R5': the supervisor's runner budget is STOP_TIMEOUT_S + 5, so _serve
        # must give the turn exactly STOP_TIMEOUT_S, read at stop time
        self.assertEqual(runner_main.STOP_TIMEOUT_S, 30.0)
        home = temp_home(self, runner="fake")
        runner = runner_main.runner_for(home)
        seen = []
        real_stop = runner.stop

        def stop(timeout=None):
            seen.append(timeout)
            return real_stop(timeout=timeout)

        runner.stop = stop
        with mock.patch.object(runner_main, "STOP_TIMEOUT_S", 7.5):
            rc = runner_main._serve(runner, True)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [7.5])


class TestRestartMark(HermeticCase):
    def test_a_claim_a_dead_runner_left_marks_the_restart(self):
        """#98: a runner killed without its teardown left its row claimed;
        the next start's sweep requeues it, and that turn was cut as much as
        a stop's: the restart mark says so to the resumed session."""
        from cousin_lib.delivery import Item
        from cousin_lib.runner import restart_note
        home = temp_home(self, runner="fake")
        runner = runner_main.runner_for(home)
        runner.inbox.put(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertEqual(len(runner.inbox.claim(limit=1, claimant="the-dead-one")), 1)
        self.assertEqual(runner_main._serve(runner, True), 0)
        note = json.loads((home / "data" / "runner-restart.json").read_text())
        self.assertIn("claimed", note["why"])
        # a clean start (nothing claimed) marks nothing
        (home / "data" / "runner-restart.json").unlink()
        self.assertEqual(runner_main._serve(runner_main.runner_for(home), True), 0)
        self.assertFalse((home / "data" / "runner-restart.json").exists())
        self.assertIsNone(restart_note.take(home))


@unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"), "claude-agent-sdk not installed")
class TestCheckAuthAndLogin(HermeticCase):
    def test_check_auth_runs_before_the_lock_the_runner_and_the_environment(self):
        from cousin_lib import accounts
        home = temp_home(self)
        for rc in (0, 4):
            out = io.StringIO()
            with mock.patch.object(accounts, "check", return_value=(rc, "account=host line")), \
                    mock.patch.object(runner_main, "runner_for") as built, \
                    mock.patch.object(runner_main, "hold_lock") as lock, \
                    mock.patch.dict(os.environ, {}), contextlib.redirect_stdout(out):
                os.environ.pop("COUSIN_HOME", None)
                self.assertEqual(runner_main.runner_main(["--home", str(home), "--check-auth"]), rc)
                self.assertNotIn("COUSIN_HOME", os.environ)          # nothing exported
            built.assert_not_called(); lock.assert_not_called()
            self.assertIn("account=host", out.getvalue())
        self.assertFalse((home / "run" / "runner.lock").exists())

    def test_validate_runs_one_bare_turn_only_after_a_good_check(self):
        from cousin_lib import accounts
        from cousin_lib.runner import sdk
        home = temp_home(self)
        with mock.patch.object(accounts, "check", return_value=(4, "account=host no")), \
                mock.patch.object(sdk, "validate_account") as val, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner_main.runner_main(["--home", str(home), "--check-auth",
                                                      "--validate"]), 4)
        val.assert_not_called()
        with mock.patch.object(accounts, "check", return_value=(0, "account=host ok")), \
                mock.patch.object(sdk, "validate_account", return_value=(0, "validate: ok")) as val, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner_main.runner_main(["--home", str(home), "--check-auth",
                                                      "--validate"]), 0)
        self.assertEqual(val.call_args[0][0].name, "host")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            runner_main.runner_main(["--home", str(home), "--validate"])   # --validate alone

    def test_validate_refuses_an_effort_the_cli_would_refuse(self):
        from cousin_lib import accounts
        from cousin_lib.runner import sdk
        home = temp_home(self)
        (home / "cousin.toml").write_text((home / "cousin.toml").read_text() + 'effort = "ludicrous"\n')
        err = io.StringIO()
        with mock.patch.object(accounts, "check", return_value=(0, "account=host ok")), \
                mock.patch.object(sdk, "validate_account") as val, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = runner_main.runner_main(["--home", str(home), "--check-auth", "--validate"])
        self.assertEqual(rc, 2)
        self.assertIn("effort", err.getvalue())
        val.assert_not_called()

    def test_a_missing_key_file_is_a_login_to_do_not_a_config_error(self):
        """Replaces test_a_missing_key_file_under_a_real_root_is_a_config_error."""
        import json
        from cousin_lib import accounts
        home = temp_home(self, runner="sdk")
        (home.parent.parent / "config").mkdir()
        _append_agent_key(home, "keys/token")
        Inbox(home).put(Item("operator:priya", "chat", "a", sender="Priya"))
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.2), \
                mock.patch.object(accounts, "status", return_value={"loggedIn": False}):
            rc, err = _run(["--home", str(home), "--once"])
        self.assertEqual(rc, 4, err)                                 # R15: never 3
        data = json.loads((home / "data" / "login-required.json").read_text())
        self.assertIn("keys/token", data["action"])

    def test_check_auth_on_a_secret_open_to_others_is_2_not_4(self):
        from cousin_lib import accounts
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir()
        (root / "keys").mkdir(); os.chmod(root / "keys", 0o700)
        (root / "keys" / "token").write_text("sk-from-file\n"); os.chmod(root / "keys" / "token", 0o644)
        _append_agent_key(home, "keys/token")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(accounts, "check") as check, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner_main.runner_main(["--home", str(home), "--check-auth"])
        self.assertEqual(rc, 2)
        check.assert_not_called()                          # configuration, before any status
        self.assertIn("chmod 600", err.getvalue())
        self.assertNotIn("sk-from-file", out.getvalue() + err.getvalue())

    def test_once_exits_4_not_3_while_a_login_is_required(self):
        runner = mock.Mock()
        runner.worker_alive.return_value = True
        runner.state.return_value = "errored"
        runner.login_required.return_value = True
        runner.inbox.unfinished.return_value = 1
        stop = mock.Mock(); stop.is_set.return_value = False
        with mock.patch.object(runner_main, "ERRORED_GIVE_UP_S", 0.05), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner_main._once(runner, stop), 4)


if __name__ == "__main__":
    unittest.main()
