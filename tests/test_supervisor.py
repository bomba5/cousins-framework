"""cousin-supervisor: the child table, the reap loop, restarts, output and
the ordered stop. Stub children are `python3 -c`
scripts; every wait has a deadline and every process is killed in
cleanup. Invented cast only."""
import contextlib
import ctypes
import io
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from cousin_lib import delivery, loops, supervisor
from cousin_lib.runner import main as runner_main
from cousin_lib.supervisor import ChildSpec, RestartPolicy, Supervisor
from tests._hermetic import HermeticCase

FAST = dict(backoff=(0.05, 0.1), window=60.0, max_exits=5, healthy_after=60.0, tick=0.02)


def _wait_for(predicate, timeout=10.0, step=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if step:
            step()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _stub(name, kind, body, *args, stop_timeout=5.0):
    return ChildSpec(name, kind, [sys.executable, "-c", textwrap.dedent(body)] + [str(a) for a in args],
                     stop_timeout=stop_timeout)


def _lines(path):
    try:
        return pathlib.Path(path).read_text().split()
    except OSError:
        return []


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _Case(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.root = self.dir / "root"
        (self.root / "config").mkdir(parents=True)

    def supervise(self, specs, **kw):
        opts = dict(FAST)
        opts.update(kw)
        out = opts.pop("out", None) or io.StringIO()
        sup = Supervisor(self.root, specs, out=out, **opts)
        self.addCleanup(self._kill_all, sup)
        sup.start_all()
        return sup, out

    def _kill_all(self, sup):
        for child in sup.children.values():
            if child.proc is not None and child.proc.returncode is None:
                try:
                    os.killpg(child.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        sup.stop_all()


# A stub that records each start (its pid) in a file, then exits with a code.
_EXIT_WITH = """
    import os, sys
    with open(sys.argv[1], "a") as fh:
        fh.write("%d\\n" % os.getpid())
    sys.exit(int(sys.argv[2]))
"""


class TestRestartPolicy(unittest.TestCase):
    def test_backoff_doubles_to_the_cap_and_resets_after_a_healthy_run(self):
        policy = RestartPolicy()        # the defaults: 1, 2, 4 ... 60 s, window 60 s, five exits
        delays = [policy.on_exit(now=100.0 * i, ran_for=1.0) for i in range(9)]
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 60, 60, 60])
        self.assertEqual(policy.on_exit(now=1000.0, ran_for=61.0), 1)   # healthy: reset
        self.assertEqual(policy.on_exit(now=1100.0, ran_for=1.0), 2)

    def test_five_exits_in_the_window_is_failing(self):
        policy = RestartPolicy()
        self.assertEqual([policy.on_exit(now=float(i), ran_for=0.1) for i in range(5)],
                         [1, 2, 4, 8, None])
        policy.reset()
        self.assertEqual(policy.on_exit(now=10.0, ran_for=0.1), 1)

    def test_a_busy_exit_backs_off_to_the_cap_and_is_never_counted(self):
        policy = RestartPolicy()
        delays = [policy.on_busy(ran_for=0.1) for _ in range(9)]
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 60, 60, 60])
        self.assertEqual(len(policy.exits), 0)
        self.assertEqual(policy.on_busy(ran_for=61.0), 1)      # a healthy run resets it too
        # busy exits leave the crash count alone: four crashes are still not failing
        self.assertNotIn(None, [policy.on_exit(now=float(i), ran_for=0.1) for i in range(4)])


class TestRestarts(_Case):
    def test_a_child_that_exits_is_restarted(self):
        starts = self.dir / "starts"
        sup, _ = self.supervise([_stub("loops", "loops", _EXIT_WITH, starts, 1)], max_exits=100)
        self.assertTrue(_wait_for(lambda: len(_lines(starts)) >= 3, step=sup.step))
        child = sup.status()["children"]["loops"]
        self.assertGreaterEqual(child["restarts"], 2)
        self.assertIn(child["state"], ("running", "backoff"))
        self.assertEqual(child["last_exit"], "code 1")

    def test_five_exits_in_a_minute_mark_failing_and_stop_restarting(self):
        starts = self.dir / "starts"
        sup, out = self.supervise([_stub("loops", "loops", _EXIT_WITH, starts, 1)],
                                  backoff=(0.01,))
        self.assertTrue(_wait_for(lambda: sup.status()["children"]["loops"]["state"] == "failing",
                                  step=sup.step))
        _wait_for(lambda: False, timeout=0.5, step=sup.step)     # time for a sixth start
        self.assertEqual(len(_lines(starts)), 5)
        child = sup.status()["children"]["loops"]
        self.assertEqual(child["reason"], "5 exits in 60s")
        self.assertIn("supervisor: loops failing: 5 exits in 60s, left down (last exit code 1)",
                      out.getvalue())

    def test_a_configuration_exit_is_failing_at_once(self):
        starts = self.dir / "starts"
        sup, out = self.supervise([_stub("runner:wren", "runner", _EXIT_WITH, starts, 2)])
        self.assertTrue(_wait_for(
            lambda: sup.status()["children"]["runner:wren"]["state"] == "failing", step=sup.step))
        _wait_for(lambda: False, timeout=0.4, step=sup.step)
        self.assertEqual(len(_lines(starts)), 1)
        self.assertEqual(sup.status()["children"]["runner:wren"]["reason"],
                         "configuration (exit 2)")
        self.assertIn("supervisor: runner:wren failing: configuration (exit 2)", out.getvalue())

    def test_a_tmux_runner_that_gave_up_is_failing_with_its_own_reason(self):
        """A tmux runner that gives up on its pane exits 2 and
        leaves data/run/tmux-giving-up.json; the failing row names why."""
        home = self.root / "cousins" / "wren"
        (home / "data" / "run").mkdir(parents=True)
        (home / "data" / "run" / "tmux-giving-up.json").write_text(json.dumps(
            {"reason": "the pane failed 5 starts in a row: exited at boot", "at": time.time()}))
        starts = self.dir / "starts"
        spec = ChildSpec("runner:wren", "runner",
                         [sys.executable, "-c", textwrap.dedent(_EXIT_WITH), str(starts), "2"],
                         stop_timeout=5.0, slug="wren", home=home)
        sup, out = self.supervise([spec])
        self.assertTrue(_wait_for(
            lambda: sup.status()["children"]["runner:wren"]["state"] == "failing", step=sup.step))
        _wait_for(lambda: False, timeout=0.4, step=sup.step)
        self.assertEqual(len(_lines(starts)), 1, "never restarted")
        reason = sup.status()["children"]["runner:wren"]["reason"]
        self.assertIn("gave up on its pane", reason)
        self.assertIn("5 starts in a row", reason)

    def test_exit_4_is_never_restarted(self):
        starts = self.dir / "starts"
        sup, out = self.supervise([_stub("runner:wren", "runner", _EXIT_WITH, starts, 4)])
        self.assertTrue(_wait_for(
            lambda: sup.status()["children"]["runner:wren"]["state"] == "stopped", step=sup.step))
        _wait_for(lambda: False, timeout=0.4, step=sup.step)
        self.assertEqual(len(_lines(starts)), 1)
        self.assertEqual(sup.status()["children"]["runner:wren"]["reason"],
                         "login required (exit 4)")

    def test_classify_exit_table(self):
        # a lock-held exit (5) is busy for a runner and for the loops
        # daemon alike: waited out after the backoff, never counted, never
        # `failing`; 2 stays configuration for every kind
        self.assertEqual(supervisor.RUNNER_BUSY_EXIT, runner_main.LOCK_HELD_EXIT)
        self.assertEqual(supervisor.LOOPS_BUSY_EXIT, loops.LOCK_HELD_EXIT)
        rows = [
            (("runner", 5), ("busy", "another runner holds its lock (exit 5)")),
            (("loops", 5), ("busy", "another loops daemon holds its lock (exit 5)")),
            (("console", 5), ("restart", None)),
            (("runner", 2), ("failing", "configuration (exit 2)")),
            (("loops", 2), ("failing", "configuration (exit 2)")),
            (("console", 2), ("failing", "configuration (exit 2)")),
            (("runner", 4), ("stopped", "login required (exit 4)")),
            (("loops", 4), ("restart", None)),
            (("console", 75), ("now", "restart requested (exit 75)")),
            (("runner", 3), ("restart", None)),
            (("loops", -9), ("restart", None)),
        ]
        for (kind, code), expected in rows:
            with self.subTest(kind=kind, code=code):
                self.assertEqual(supervisor.classify_exit(kind, code), expected)

    def _busy_six_times(self, name, kind, holder):
        """A stub that exits 5 on its first six starts (a holder has the
        lock), then runs: six busy exits inside a minute, where five
        counted exits would be `failing`."""
        starts = self.dir / "starts"
        body = """
            import os, sys, time
            with open(sys.argv[1], "a") as fh:
                fh.write("%d\\n" % os.getpid())
            if len(open(sys.argv[1]).read().split()) <= 6:
                sys.exit(5)
            time.sleep(30)
        """
        sup, out = self.supervise([_stub(name, kind, body, starts)])   # FAST: 0.05, 0.1 s
        states = set()

        def _watch():
            sup.step()
            states.add(sup.status()["children"][name]["state"])
        self.assertTrue(_wait_for(lambda: len(_lines(starts)) >= 7, step=_watch),
                        out.getvalue())
        self.assertTrue(_wait_for(
            lambda: sup.status()["children"][name]["state"] == "running", step=_watch))
        self.assertNotIn("failing", states, out.getvalue())
        self.assertIn("backoff", states)
        child = sup.status()["children"][name]
        self.assertEqual(child["restarts"], 6)
        self.assertEqual(child["last_exit"], "code 5")
        self.assertEqual(len(sup.children[name].policy.exits), 0)   # none counted
        self.assertEqual(out.getvalue().count(
            "supervisor: %s busy: another %s holds its lock (exit 5), retrying in" % (name, holder)),
            6, out.getvalue())
        self.assertIn("; not counted toward failing", out.getvalue())
        self.assertNotIn("supervisor: %s failing" % name, out.getvalue())

    def test_a_busy_runner_waits_in_backoff_uncounted_and_runs_when_free(self):
        self._busy_six_times("runner:wren", "runner", "runner")

    def test_a_busy_loops_daemon_waits_in_backoff_uncounted_and_runs_when_free(self):
        self._busy_six_times("loops", "loops", "loops daemon")

    def test_a_busy_child_says_why_while_it_waits(self):
        starts = self.dir / "starts"
        sup, _ = self.supervise([_stub("loops", "loops", _EXIT_WITH, starts, 5)],
                                backoff=(30.0,))
        self.assertTrue(_wait_for(
            lambda: sup.status()["children"]["loops"]["state"] == "backoff", step=sup.step))
        self.assertEqual(sup.status()["children"]["loops"]["reason"],
                         "another loops daemon holds its lock (exit 5)")

    def test_exit_3_restarts_a_runner(self):
        starts = self.dir / "starts"
        sup, _ = self.supervise([_stub("runner:wren", "runner", _EXIT_WITH, starts, 3)])
        self.assertTrue(_wait_for(lambda: len(_lines(starts)) >= 2, step=sup.step))

    def test_console_exit_75_restarts_at_once_uncounted(self):
        self.assertEqual(supervisor.CONSOLE_RESTART_EXIT, 75)
        from cousin_lib.console.routes_admin import RESTART_EXIT_CODE
        self.assertEqual(supervisor.CONSOLE_RESTART_EXIT, RESTART_EXIT_CODE)
        starts = self.dir / "starts"
        body = """
            import os, sys, time
            with open(sys.argv[1], "a") as fh:
                fh.write("%d\\n" % os.getpid())
            if len(open(sys.argv[1]).read().split()) <= 6:
                sys.exit(75)
            time.sleep(30)
        """
        # a backoff restart would take 5 s each: seven starts in 3 s are all "at once",
        # and six exits past max_exits=5 prove they were not counted
        sup, _ = self.supervise([_stub("console", "console", body, starts)], backoff=(5.0,))
        self.assertTrue(_wait_for(lambda: len(_lines(starts)) >= 7, timeout=3.0, step=sup.step))
        child = sup.status()["children"]["console"]
        self.assertEqual(child["state"], "running")
        self.assertEqual(child["restarts"], 6)


class TestChildren(_Case):
    def test_lines_are_prefixed_with_the_child_name(self):
        body = """
            import sys, time
            print("hello from Wren", flush=True)
            print("to stderr", file=sys.stderr, flush=True)
            sys.stdout.write("partial")
            sys.stdout.flush()
            sys.exit(0)
        """
        sup, out = self.supervise([_stub("runner:wren", "runner", body)], backoff=(30.0,))
        self.assertTrue(_wait_for(lambda: "runner:wren | partial\n" in out.getvalue(),
                                  step=sup.step))
        text = out.getvalue()
        self.assertIn("runner:wren | hello from Wren\n", text)
        self.assertIn("runner:wren | to stderr\n", text)
        self.assertRegex(text, r"supervisor: started runner:wren \(pid \d+\)")

    def test_children_run_in_their_own_session(self):
        seen = self.dir / "seen"
        body = """
            import os, sys, time
            with open(sys.argv[1] + ".tmp", "w") as fh:
                fh.write("%d %d %d" % (os.getpid(), os.getsid(0), os.getpgid(0)))
            os.rename(sys.argv[1] + ".tmp", sys.argv[1])
            time.sleep(30)
        """
        sup, _ = self.supervise([_stub("loops", "loops", body, seen)])
        self.assertTrue(_wait_for(seen.exists, step=sup.step))
        pid, sid, pgid = (int(x) for x in seen.read_text().split())
        self.assertEqual(sid, pid)
        self.assertEqual(pgid, pid)
        self.assertNotEqual(sid, os.getsid(0))

    def test_child_env_marks_it_supervised(self):
        seen = self.dir / "seen.json"
        body = """
            import json, os, sys, time
            keys = ("COUSIN_SUPERVISED", "FRAMEWORK_ROOT", "PYTHONUNBUFFERED")
            with open(sys.argv[1] + ".tmp", "w") as fh:
                json.dump({k: os.environ.get(k) for k in keys}, fh)
            os.rename(sys.argv[1] + ".tmp", sys.argv[1])
            time.sleep(30)
        """
        sup, _ = self.supervise([_stub("loops", "loops", body, seen)])
        self.assertTrue(_wait_for(seen.exists, step=sup.step))
        self.assertEqual(json.loads(seen.read_text()),
                         {"COUSIN_SUPERVISED": "1", "FRAMEWORK_ROOT": str(self.root),
                          "PYTHONUNBUFFERED": "1"})

    def test_specs_run_each_module_with_this_interpreter(self):
        console = supervisor.console_spec("/srv/fw", "0.0.0.0", 8600)
        self.assertEqual(console.argv, [sys.executable, "-m", "cousin_lib.console.app", "serve",
                                        "--root", "/srv/fw", "--host", "0.0.0.0", "--port", "8600"])
        self.assertEqual((console.name, console.kind, console.stop_timeout),
                         ("console", "console", 10.0))
        loops = supervisor.loops_spec("/srv/fw", 30)
        self.assertEqual(loops.argv, [sys.executable, "-m", "cousin_lib.loops", "run",
                                      "--interval", "30"])
        self.assertEqual((loops.name, loops.stop_timeout), ("loops", 10.0))
        runner = supervisor.runner_spec("/srv/fw/cousins/wren")
        self.assertEqual(runner.argv, [sys.executable, "-m", "cousin_lib.runner.main",
                                       "--home", "/srv/fw/cousins/wren"])
        self.assertEqual((runner.name, runner.kind, runner.slug, runner.stop_timeout),
                         ("runner:wren", "runner", "wren", 35.0))
        # one constant; the supervisor's budget is the runner's own + 5 s
        self.assertEqual(runner_main.STOP_TIMEOUT_S, 30.0)
        self.assertEqual(supervisor.STOP_TIMEOUTS["runner"], runner_main.STOP_TIMEOUT_S + 5)

    def test_runner_kinds_have_one_source(self):
        # delivery's list is the one the supervisor and the runner read
        self.assertEqual(delivery.RUNNER_KINDS, ("sdk", "fake", "opencode", "tmux"))
        self.assertIs(supervisor.RUNNER_KINDS, delivery.RUNNER_KINDS)
        self.assertIs(runner_main.KINDS, delivery.RUNNER_KINDS)

    def test_the_loops_module_runs_as_a_child(self):
        # `python -m cousin_lib.loops` must be the CLI, or the loops child
        # exits 0 at once, silently, and is restarted into `failing`
        proc = subprocess.run([sys.executable, "-m", "cousin_lib.loops", "--help"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("cousin-loops", proc.stdout)


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None


class TestReap(_Case):
    def test_a_child_restarted_inside_the_reap_loop_is_not_an_orphan(self):
        # the console's 75 restarts inside reap(); a new child that dies
        # before the loop drains is still that child, not an orphan
        sup = Supervisor(self.root, [_stub("console", "console", "pass")],
                         out=io.StringIO(), **FAST)
        child = sup.children["console"]
        pids = iter([4101, 4102])

        def fake_start(c):
            c.proc = _FakeProc(next(pids))
            c.started_at = sup.clock()
            c.set_state("running")

        statuses = [(4101, 75 << 8), (4102, 1 << 8), (0, 0)]
        with mock.patch.object(sup, "_start", side_effect=fake_start), \
                mock.patch.object(supervisor.os, "waitpid",
                                  side_effect=lambda pid, flags: statuses.pop(0)):
            sup._start(child)
            sup.reap()
        row = sup.status()["children"]["console"]
        self.assertEqual(statuses, [])
        self.assertEqual((row["state"], row["pid"], row["last_exit"], row["restarts"]),
                         ("backoff", None, "code 1", 1))


# ------------------------------------------------------------------ process level

_DRIVER = """
import ctypes, json, sys
from cousin_lib import supervisor as sv
cfg = json.loads(sys.argv[1])
if cfg.get("subreaper"):
    # what PID 1 is: orphans of our children are reparented to us
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        sys.exit(9)
specs = [sv.ChildSpec(**s) for s in cfg["specs"]]
sup = sv.Supervisor(cfg["root"], specs, backoff=tuple(cfg.get("backoff", (0.05,))), tick=0.02)
sys.exit(sup.serve())
"""

# Records the SIGTERM it receives (its name, appended to a file), waits
# `delay` seconds, records "<name>:done" and exits 0.
_TERM_RECORDER = """
    import os, signal, sys, time
    name, order, ready, delay = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    def on_term(signum, frame):
        with open(order, "a") as fh:
            fh.write(name + "\\n")
        time.sleep(delay)
        with open(order, "a") as fh:
            fh.write(name + ":done\\n")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    with open(ready, "a") as fh:
        fh.write("%s %d\\n" % (name, os.getpid()))
    while True:
        time.sleep(1)
"""


def _spec_dict(name, kind, body, *args, stop_timeout=5.0):
    s = _stub(name, kind, body, *args, stop_timeout=stop_timeout)
    return {"name": s.name, "kind": s.kind, "argv": s.argv, "stop_timeout": s.stop_timeout}


class TestProcess(_Case):
    def drive(self, specs, **cfg):
        cfg.update(root=str(self.root), specs=specs)
        log = open(self.dir / "driver.log", "w")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-c", _DRIVER, json.dumps(cfg)],
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

        def _cleanup():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
            for line in _lines(self.dir / "ready")[1::2]:
                try:
                    os.kill(int(line), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
        self.addCleanup(_cleanup)
        return proc

    def output(self):
        return (self.dir / "driver.log").read_text()

    def _recorders(self, names_kinds, delay=0.0, stop_timeout=5.0):
        order, ready = self.dir / "order", self.dir / "ready"
        return [_spec_dict(n, k, _TERM_RECORDER, n, order, ready, delay, stop_timeout=stop_timeout)
                for n, k in names_kinds], order, ready

    def test_sigterm_stops_children_in_reverse_order_and_exits_0(self):
        specs, order, ready = self._recorders([("console", "console"), ("loops", "loops"),
                                               ("runner:sam", "runner"), ("runner:wren", "runner")])
        proc = self.drive(specs)
        self.assertTrue(_wait_for(lambda: len(_lines(ready)) == 8))
        started = re.findall(r"supervisor: started (\S+) \(pid", self.output())
        self.assertEqual(started, ["console", "loops", "runner:sam", "runner:wren"])
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(20), 0)
        got = [line for line in _lines(order) if not line.endswith(":done")]
        self.assertEqual(sorted(got[:2]), ["runner:sam", "runner:wren"])
        self.assertEqual(got[2:], ["loops", "console"])
        self.assertIn("supervisor: stopped", self.output())

    def test_sigterm_waits_for_a_slow_runner_within_its_timeout(self):
        specs, order, ready = self._recorders([("console", "console"), ("runner:wren", "runner")],
                                              delay=2.0, stop_timeout=10.0)
        proc = self.drive(specs)
        self.assertTrue(_wait_for(lambda: len(_lines(ready)) == 4))
        t = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(30), 0)
        self.assertGreaterEqual(time.monotonic() - t, 2.0)
        # the runner finished before the console was even asked to stop
        self.assertEqual(_lines(order), ["runner:wren", "runner:wren:done",
                                         "console", "console:done"])
        self.assertNotIn("killed", self.output())

    def test_a_child_that_ignores_sigterm_is_killed_after_its_timeout(self):
        ready = self.dir / "ready"
        body = """
            import os, signal, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            with open(sys.argv[1], "a") as fh:
                fh.write("runner:wren %d\\n" % os.getpid())
            while True:
                time.sleep(1)
        """
        proc = self.drive([_spec_dict("runner:wren", "runner", body, ready, stop_timeout=1.0)])
        self.assertTrue(_wait_for(lambda: len(_lines(ready)) == 2))
        pid = int(_lines(ready)[1])
        t = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(20), 0)
        self.assertGreaterEqual(time.monotonic() - t, 1.0)
        self.assertLess(time.monotonic() - t, 8.0)
        self.assertFalse(_alive(pid))
        self.assertIn("supervisor: runner:wren did not stop within 1s, killed", self.output())

    @unittest.skipUnless(sys.platform.startswith("linux"), "prctl is Linux-only")
    def test_an_orphan_of_a_child_is_reaped(self):
        try:
            ctypes.CDLL(None).prctl
        except (OSError, AttributeError):
            self.skipTest("prctl unavailable")
        grandchild = self.dir / "grandchild"
        body = """
            import os, sys, time
            path = sys.argv[1]
            if os.path.exists(path):
                time.sleep(60)                  # a restart: nothing more to fork
            pid = os.fork()
            if pid == 0:
                time.sleep(1.0)                 # outlives its parent, then exits
                os._exit(0)
            with open(path + ".tmp", "w") as fh:
                fh.write(str(pid))
            os.rename(path + ".tmp", path)
            sys.exit(0)                         # the grandchild is now an orphan
        """
        proc = self.drive([_spec_dict("runner:wren", "runner", body, grandchild)],
                          subreaper=True, backoff=[0.05])
        self.assertTrue(_wait_for(grandchild.exists))
        gpid = int(grandchild.read_text())
        self.addCleanup(lambda: _alive(gpid) and os.kill(gpid, signal.SIGKILL))
        stat = pathlib.Path("/proc/%d/stat" % gpid)

        # a zombie keeps its /proc entry (state Z) until its parent reaps it
        self.assertTrue(_wait_for(lambda: not stat.exists(), timeout=10.0),
                        "the orphan %d was left a zombie" % gpid)
        self.assertIsNone(proc.poll())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(60), 0)     # a loaded runner: the stop, not its speed


class TestUmask(_Case):
    """`run` sets umask 077 before it writes anything, every child inherits
    it, and the caller's umask is back once `run` returns."""

    _PRINT_UMASK = "import os; print(oct(os.umask(0)))"

    def _run(self, serve):
        before = os.umask(0o022)
        self.addCleanup(os.umask, before)
        with mock.patch.object(supervisor.Supervisor, "serve", serve), \
                contextlib.redirect_stdout(io.StringIO()):
            code = supervisor.supervisor_main(["run", "--root", str(self.root),
                                               "--no-console", "--no-loops"])
        return code

    def test_the_umask_is_077(self):
        self.assertEqual(supervisor.UMASK, 0o077)

    def test_apply_umask_sets_it_and_returns_the_previous_one(self):
        before = os.umask(0o022)
        self.addCleanup(os.umask, before)
        self.assertEqual(supervisor.apply_umask(), 0o022)
        self.assertEqual(os.umask(0o022), 0o077)

    def test_run_serves_under_077_and_a_child_inherits_it(self):
        seen = {}

        def serve(sup):
            current = os.umask(0)
            os.umask(current)
            seen["own"] = current
            seen["child"] = subprocess.run([sys.executable, "-c", self._PRINT_UMASK],
                                           capture_output=True, text=True,
                                           timeout=60).stdout.strip()
            return 0

        self.assertEqual(self._run(serve), 0)
        self.assertEqual(seen, {"own": 0o077, "child": "0o77"})
        self.assertEqual(os.umask(0o022), 0o022)    # put back on return

    def test_what_run_seeds_before_serving_is_written_under_it(self):
        self.assertEqual(self._run(lambda sup: 0), 0)
        seeded = sorted((self.root / "shared").glob("*.md"))
        self.assertTrue(seeded)
        for path in seeded:
            self.assertEqual(path.stat().st_mode & 0o077, 0, path)


if __name__ == "__main__":
    unittest.main()
