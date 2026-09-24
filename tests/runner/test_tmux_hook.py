"""The tmux kind's pane hook (phase 11 R6, R19, R24, M-a): it records
SessionStart's session for the runner, wakes the runner on every event,
and never blocks or fails the CLI that runs it."""
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from cousin_lib import harness_settings
from cousin_lib.delivery import Item
from cousin_lib.runner import tmux_hook, tmux_launch, wake
from cousin_lib.runner import tmux_runner
from cousin_lib.runner.tmux_runner import TmuxRunner
from tests._hermetic import HermeticCase
from tests.runner._fake_pane import FakePane
from tests.runner._home import temp_home

REPO = Path(__file__).resolve().parents[2]
SID = "0f3c6a52-9d1e-4b7a-8c2f-5e6d7a8b9c01"


def _wait(pred, timeout=5.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _start(sid=SID, transcript="/tmp/t.jsonl", source="startup"):
    return json.dumps({"session_id": sid, "transcript_path": transcript, "cwd": "/x",
                       "hook_event_name": "SessionStart", "source": source})


class Case(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="tmux")
        # this process plays the pane's CLI: the launcher exports its pid
        os.environ[tmux_hook.PANE_PID_VAR] = str(tmux_hook.cli_pid())

    def hook(self, event, stdin):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = tmux_hook.main(["--home", str(self.home), event], stdin=stdin)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "", "a hook's stdout reaches the model's context")

    def record(self):
        return json.loads((self.home / "run" / "tmux-session.json").read_text())


class TestSessionStart(Case):
    def test_the_record_is_written_with_the_clis_pid(self):
        self.hook("SessionStart", _start(transcript="/p/s.jsonl", source="resume"))
        rec = self.record()
        self.assertEqual(rec, {"session_id": SID, "transcript_path": "/p/s.jsonl",
                               "source": "resume", "pid": rec["pid"]})
        self.assertIsInstance(rec["pid"], int)

    def test_the_record_is_0600_and_replaced_whole(self):
        path = self.home / "run" / "tmux-session.json"
        path.write_text("an older record, longer than the new one " * 10)
        self.hook("SessionStart", _start())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.record()["session_id"], SID)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                         ["tmux-session.json"], "no temp file left behind")

    def test_a_missing_run_dir_is_created_0700(self):
        (self.home / "run").rmdir()
        self.hook("SessionStart", _start())
        self.assertEqual(stat.S_IMODE((self.home / "run").stat().st_mode), 0o700)
        self.assertEqual(self.record()["session_id"], SID)

    def test_other_events_write_no_record(self):
        self.hook("UserPromptSubmit", json.dumps({"session_id": SID, "prompt": "hi",
                                                  "transcript_path": "/p/s.jsonl"}))
        self.assertFalse((self.home / "run" / "tmux-session.json").exists())

    def test_bad_input_is_ignored_with_exit_0(self):
        for stdin in ("", "not json", "[1, 2]", json.dumps({"session_id": 7}),
                      json.dumps({"transcript_path": "/p"})):
            self.hook("SessionStart", stdin)
        self.assertFalse((self.home / "run" / "tmux-session.json").exists())

    def test_bad_arguments_exit_0(self):
        for argv in ([], ["--home"], ["SessionStart"], ["--nope", "x"]):
            self.assertEqual(tmux_hook.main(argv, stdin=_start()), 0)

    def test_a_cli_that_is_not_the_panes_writes_and_sends_nothing(self):
        # an operator's claude in the home (no variable), a claude the pane's
        # model started (the variable inherited, a pid of its own)
        for value in (None, "", "junk", str(tmux_hook.cli_pid() + 1)):
            if value is None:
                os.environ.pop(tmux_hook.PANE_PID_VAR, None)
            else:
                os.environ[tmux_hook.PANE_PID_VAR] = value
            with wake.Listener(self.home) as listener:
                self.hook("SessionStart", _start())
                self.assertFalse(listener.wait(timeout=0.1), value)
            self.assertFalse((self.home / "run" / "tmux-session.json").exists(), value)

    def test_the_hard_bound_removes_the_temp_record_it_made(self):
        class Exited(BaseException):
            pass

        def alarm_mid_write(src, dst):          # SIGALRM lands between write and replace
            self.assertTrue(Path(src).exists())
            tmux_hook._expired(None, None)
        with mock.patch.object(tmux_hook.os, "replace", alarm_mid_write), \
                mock.patch.object(tmux_hook.os, "_exit", side_effect=Exited):
            with self.assertRaises(Exited):
                tmux_hook._write_record(self.home, {"session_id": SID})
        self.assertEqual(list((self.home / "run").iterdir()), [])

    def test_a_home_that_does_not_exist_is_not_created(self):
        gone = self.home.parent / "nobody"
        self.assertEqual(tmux_hook.main(["--home", str(gone), "SessionStart"], stdin=_start()), 0)
        self.assertFalse(gone.exists())


class TestDatagram(Case):
    def test_no_listener_exits_0(self):
        for event in tmux_hook.EVENTS:
            self.hook(event, json.dumps({"session_id": SID}))

    def test_every_event_wakes_the_listener_with_its_session(self):
        with wake.Listener(self.home) as listener:
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "Notification"):
                self.hook(event, _start() if event == "SessionStart" else json.dumps(
                    {"session_id": SID, "notification_type": "permission_prompt"}))
                self.assertTrue(listener.wait(timeout=1.0), event)
                extra = {"Notification": {"type": "permission_prompt"},
                         "SessionStart": {"source": "startup"}}.get(event, {})
                self.assertEqual([json.loads(m) for m in listener.messages],
                                 [dict({"event": event, "session_id": SID}, **extra)])

    def test_an_idle_prompt_notification_is_ignored(self):
        with wake.Listener(self.home) as listener:
            self.hook("Notification", json.dumps({"session_id": SID,
                                                  "notification_type": "idle_prompt"}))
            self.assertFalse(listener.wait(timeout=0.2))


class TestPaneEnvironment(Case):
    """M-a: the hook runs as the CLI's child, in the environment the
    launcher built (`env -i` plus the allowlist): no PYTHONPATH, no
    FRAMEWORK_ROOT, no COUSIN_*."""

    def launcher_env(self):
        env = tmux_launch.env_base(dict(os.environ))
        env.update(tmux_launch.SWITCHES)
        env[tmux_hook.PANE_PID_VAR] = str(os.getpid())    # this process plays the pane's CLI
        self.assertNotIn("PYTHONPATH", env)
        return env

    def home_resolves_this_tree(self):
        """Skip unless `import cousin_lib`, from the home under the pane's
        environment, finds THIS checkout: the live entry must never pass
        green on another tree's code (a worktree under a venv whose
        editable install points at main)."""
        proc = self.run_hook([sys.executable, "-c",
                              "import cousin_lib, os; print(os.path.dirname(cousin_lib.__file__))"],
                             "", cwd=self.home)
        self.assertEqual(proc.returncode, 0, "cousin_lib does not import from the home with no"
                         " PYTHONPATH (M-a): %s" % proc.stderr)
        found = Path(proc.stdout.strip()).resolve()
        if found != (REPO / "cousin_lib").resolve():
            self.skipTest("from the home, %s imports cousin_lib from %s, not this tree %s"
                          % (sys.executable, found, REPO))

    def test_the_live_hook_command_from_the_home(self):
        """The command harness_settings writes, as the CLI runs it: through
        /bin/sh, from the cousin home, in the launcher's environment."""
        self.home_resolves_this_tree()
        hooks = harness_settings._tmux_hooks(self.home, sys.executable)
        (entry,) = hooks["SessionStart"][0]["hooks"]
        proc = self.run_hook(["/bin/sh", "-c", entry["command"]], _start(), cwd=self.home)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(self.record()["session_id"], SID)
        self.assertEqual(self.record()["pid"], os.getpid())

    def run_hook(self, argv, stdin, cwd):
        env = self.launcher_env()
        cmd = ["env", "-i"] + ["%s=%s" % kv for kv in sorted(env.items())] + argv
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              cwd=cwd, timeout=30)

    def test_the_hook_command_runs_under_env_i(self):
        # cwd is this checkout, so the module under test is this tree's
        proc = self.run_hook([sys.executable, "-m", "cousin_lib.runner.tmux_hook",
                              "--home", str(self.home), "SessionStart"], _start(), cwd=REPO)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(self.record()["session_id"], SID)
        self.assertEqual(self.record()["pid"], os.getpid(), "env execs python: we are its parent")

    def test_a_shell_between_the_cli_and_the_hook_is_skipped(self):
        line = shlex.join([sys.executable, "-m", "cousin_lib.runner.tmux_hook",
                           "--home", str(self.home), "SessionStart"]) + "; true"
        proc = self.run_hook(["/bin/sh", "-c", line], _start(), cwd=REPO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.record()["pid"], os.getpid())

    def test_the_hook_module_imports_from_the_home_with_no_pythonpath(self):
        # the CLI runs hooks from the cousin home: the interpreter's own
        # install must find the hook there (M-a's silent failure)
        self.home_resolves_this_tree()
        proc = self.run_hook([sys.executable, "-c", "import cousin_lib.runner.tmux_hook"], "",
                             cwd=self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_module_imports_nothing_heavy(self):
        code = ("import json, sys, cousin_lib.runner.tmux_hook; "
                "print(json.dumps(sorted(n for n in sys.modules if n.startswith('cousin_lib'))))")
        proc = self.run_hook([sys.executable, "-c", code], "", cwd=REPO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout),
                         ["cousin_lib", "cousin_lib.runner", "cousin_lib.runner.tmux_hook"])

    def test_stdin_left_open_does_not_hang_the_hook(self):
        env = self.launcher_env()
        cmd = ["env", "-i"] + ["%s=%s" % kv for kv in sorted(env.items())] + [
            sys.executable, "-m", "cousin_lib.runner.tmux_hook", "--home", str(self.home), "Stop"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=REPO)
        try:
            t = time.monotonic()
            rc = proc.wait(timeout=15)          # stdin is never written nor closed
            self.assertEqual(rc, 0)
            self.assertLess(time.monotonic() - t, tmux_hook.HARD_S + 2)
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestRunnerSide(Case):
    def runner(self, **kw):
        panes = []

        def factory(path):
            p = FakePane(path, **kw)
            panes.append(p)
            return p
        r = TmuxRunner(self.home, account=None, pane_factory=factory,
                       config_dir=self.home / ".cfg",
                       launch_argv=lambda sid, fresh: ["claude", sid])
        self.addCleanup(lambda: r.stop(timeout=5))
        self.panes = panes
        return r

    def recorded(self, sid):
        (self.home / "data" / "runner-session.json").write_text(json.dumps(
            {"session_id": sid, "lane": None, "generation": 1, "updated": 0, "kind": "tmux"}))

    def test_the_runner_reads_the_transcript_path_the_hook_recorded(self):
        path = self.home / "elsewhere" / ("%s.jsonl" % SID)
        self.recorded(SID)
        self.hook("SessionStart", _start(transcript=str(path)))
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertEqual(r._path, path)
        self.assertEqual(r._hook_path(), path)

    def test_a_record_of_another_session_is_not_used(self):
        self.recorded(SID)
        self.hook("SessionStart", _start(sid="another", transcript="/p/other.jsonl"))
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertIsNone(r._hook_path())
        self.assertNotEqual(r._path, Path("/p/other.jsonl"))

    def silent(self, r):
        return [e for e in r.events() if e["kind"] == "system"
                and e["payload"].get("subtype") == "hooks_silent"]

    def test_no_datagram_by_the_first_turn_end_is_said_once(self):
        r = self.runner()
        r.hooks_silent_s = 0.0
        r.start()
        for n in range(2):
            rec = r.enqueue(Item("operator:wren", "chat", "turn %d" % n, sender="Wren"))
            self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertTrue(_wait(lambda: self.silent(r)))
        time.sleep(0.3)
        self.assertEqual(len(self.silent(r)), 1)

    def test_the_silence_says_its_bound(self):
        r = self.runner()
        r.hooks_silent_s = 0.0
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertTrue(_wait(lambda: self.silent(r)))
        self.assertIn("within 0 s of the first turn end", self.silent(r)[0]["payload"]["detail"])
        self.assertEqual(tmux_runner.HOOKS_SILENT_S, 5.0)

    def test_a_runner_without_its_socket_does_not_say_hooks_silent(self):
        with mock.patch.object(wake, "listen", lambda home, on_error: wake.Poller()):
            r = self.runner()
            r.hooks_silent_s = 0.0
            r.start()
            rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
            self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered",
                                  timeout=10))
            time.sleep(0.5)
        self.assertEqual(self.silent(r), [])

    def test_a_hook_datagram_of_this_session_keeps_it_quiet(self):
        r = self.runner(on_prompt=lambda pane, first, body: self.hook(
            "UserPromptSubmit", json.dumps({"session_id": r.session_id()})))
        r.hooks_silent_s = 0.0
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        time.sleep(0.4)
        self.assertEqual(self.silent(r), [])

    def test_a_datagram_of_another_session_does_not_count(self):
        r = self.runner(on_prompt=lambda pane, first, body: self.hook(
            "UserPromptSubmit", json.dumps({"session_id": "not-this-one"})))
        r.hooks_silent_s = 0.0
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertTrue(_wait(lambda: self.silent(r)))


class TestAdopt(Case):
    """R24: a live pane is adopted only when the hook's record names the
    recorded session and the pane's CLI pid; otherwise it is killed and the
    session resumed in a new pane, and the refusal is said."""

    def setUp(self):
        super().setUp()
        (self.home / "data" / "runner-session.json").write_text(json.dumps(
            {"session_id": SID, "lane": None, "generation": 1, "updated": 0, "kind": "tmux"}))
        self.live = None

    def runner(self, linger=None):
        def factory(path):
            if self.live is None:                  # the previous runner's pane, still running
                self.live = FakePane(path, linger=linger)
                self.live.start(["claude", SID], cwd=str(self.home), env_base={})
            return self.live
        r = TmuxRunner(self.home, account=None, pane_factory=factory,
                       config_dir=self.home / ".cfg",
                       launch_argv=lambda sid, fresh: ["claude", sid])
        r.kill_grace_s, r.kill_bound_s = 0.2, 0.8
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def write_record(self, sid=SID, pid=4242):
        (self.home / "run" / "tmux-session.json").write_text(json.dumps(
            {"session_id": sid, "transcript_path": str(self.home / "t" / "s.jsonl"),
             "source": "startup", "pid": pid}))

    def opened(self, r):
        self.assertTrue(_wait(lambda: any(e["kind"] == "session" for e in r.events())))
        return next(e["payload"] for e in r.events() if e["kind"] == "session")

    def refusals(self, r):
        return [e["payload"] for e in r.events() if e["kind"] == "system"
                and e["payload"].get("subtype") == "adopt_refused"]

    def test_a_matching_record_adopts_the_pane(self):
        self.write_record()
        r = self.runner()
        r.start()
        self.assertEqual(self.opened(r)["source"], "adopted")
        self.assertEqual((self.live.kills, len(self.live.started)), (0, 1))
        self.assertEqual(self.refusals(r), [])

    def assert_refused(self, reason):
        r = self.runner()
        r.start()
        self.assertEqual(self.opened(r)["source"], "resumed")
        self.assertEqual(self.live.kills, 1, "the old pane is killed")
        self.assertEqual(len(self.live.started), 2, "and the session resumed in a new one")
        self.assertEqual(self.live.started[-1][0], ["claude", SID])
        refused = self.refusals(r)
        self.assertEqual([x["reason"] for x in refused], [reason])
        self.assertEqual(refused[0]["session_id"], SID)
        return refused[0]

    def test_no_record_refuses_the_adopt(self):
        self.assert_refused("no_record")

    def test_a_record_of_another_session_refuses_the_adopt(self):
        self.write_record(sid="another-session")
        self.assertEqual(self.assert_refused("session_mismatch")["record_session_id"],
                         "another-session")

    def test_a_cli_that_outlives_the_kill_is_sigkilled_before_the_resume(self):
        r = self.runner(linger="until_sigkill")
        r.start()
        self.assertEqual(self.opened(r)["source"], "resumed")
        self.assertEqual(self.live.sigkills, [4242])
        self.assertEqual(len(self.live.started), 2)

    def test_a_cli_that_outlives_the_sigkill_gets_no_second_writer(self):
        r = self.runner(linger="forever")
        r.start()
        errors = lambda: [e["payload"]["error"] for e in r.events() if e["kind"] == "error"]
        # _run says errored a moment before it appends the error: wait for the error
        self.assertTrue(_wait(lambda: any("outlived its kill" in e and SID in e
                                          for e in errors())), errors())
        self.assertEqual(r.state(), "errored")
        self.assertEqual(len(self.live.started), 1, "no second CLI on the same session")
        self.assertEqual(self.live.sigkills, [4242])
        self.assertFalse([e for e in r.events() if e["kind"] == "session"])

    def test_a_record_of_another_pid_refuses_the_adopt(self):
        self.write_record(pid=999999)
        refused = self.assert_refused("pid_mismatch")
        self.assertEqual((refused["record_pid"], refused["pane_pid"]), (999999, 4242))


class TestSessionChanged(Case):
    """A /clear in the pane starts a new session the runner does not follow
    (a known gap): it is said, once per new id, never acted on."""

    def test_a_session_start_of_another_id_is_said_once(self):
        r = TestRunnerSide.runner(self)
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        for _ in range(2):
            self.hook("SessionStart", _start(sid="after-clear", source="clear"))
        changed = lambda: [e["payload"] for e in r.events() if e["kind"] == "system"
                           and e["payload"].get("subtype") == "session_changed"]
        self.assertTrue(_wait(changed))
        time.sleep(0.3)
        self.assertEqual(changed(), [{"subtype": "session_changed", "session_id": r.session_id(),
                                      "new_session_id": "after-clear", "source": "clear"}])
        self.assertNotEqual(r.session_id(), "after-clear", "the runner does not follow it")

    def test_a_datagram_of_the_wrong_shape_is_not_heard(self):
        r = TestRunnerSide.runner(self)
        r.hooks_silent_s = 0.0
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        sid = r.session_id()
        for bad in ({"event": "Nope", "session_id": sid},
                    {"event": "Stop", "session_id": 7},
                    {"event": "Stop", "session_id": sid + "x" * 200},
                    {"event": "SessionStart", "session_id": "x" * 200, "source": "clear"},
                    {"event": "SessionStart", "session_id": "other", "source": ["clear"]},
                    {"event": "SessionStart", "session_id": "other", "source": "c" * 40},
                    ["Stop", sid]):
            wake.send(self.home, json.dumps(bad).encode())
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertTrue(_wait(lambda: any(e["payload"].get("subtype") == "hooks_silent"
                                          for e in r.events() if e["kind"] == "system")))
        self.assertFalse([e for e in r.events() if e["kind"] == "system"
                          and e["payload"].get("subtype") == "session_changed"])

    def test_the_ids_said_are_bounded(self):
        r = TestRunnerSide.runner(self)
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        for n in range(tmux_runner.CHANGES_KEPT + 10):
            wake.send(self.home, json.dumps({"event": "SessionStart", "session_id": "s-%d" % n,
                                             "source": "clear"}).encode())
            time.sleep(0.01)
        said = lambda: [e for e in r.events() if e["kind"] == "system"
                        and e["payload"].get("subtype") == "session_changed"]
        self.assertTrue(_wait(lambda: len(said()) == tmux_runner.CHANGES_KEPT + 10))
        self.assertEqual(len(r._changes_said), tmux_runner.CHANGES_KEPT)

    def test_a_resume_of_this_session_is_not_a_change(self):
        r = TestRunnerSide.runner(self)
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.hook("SessionStart", _start(sid=r.session_id(), source="resume"))
        time.sleep(0.4)
        self.assertFalse([e for e in r.events() if e["kind"] == "system"
                          and e["payload"].get("subtype") == "session_changed"])


if __name__ == "__main__":
    unittest.main()
