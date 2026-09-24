"""cousin-supervisor runs a runner cousin's Telegram bridge (#101, R10):
a `telegram:<slug>` child beside `runner:<slug>` when `[telegram]` is
enabled and valid (telegram.load_bridge_config's rule), started after
its runner, stopped and held with it, restarted with the backoff, added
and removed by a rescan, never a second bridge. The supervisor runs in
process; the runner and the bridge are `python3 -c` stubs (the bridge's
argv is patched: nothing here reaches Telegram). Invented cast only."""
import io
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from cousin_lib import supervisor
from cousin_lib.supervisor import ChildSpec, Supervisor
from tests._hermetic import HermeticCase

FAST = dict(backoff=(0.05, 0.1), window=60.0, max_exits=5, healthy_after=60.0, tick=0.02)
TOKEN = "123456789:" + "C" * 35

# The bridge stub: records each start (its pid), then sleeps, or exits
# with the code in its home's data/bridge-exit when that file exists.
_BRIDGE = """
    import os, pathlib, sys, time
    home = pathlib.Path(sys.argv[1])
    with open(home / "data" / "bridge-starts", "a") as fh:
        fh.write("%d\\n" % os.getpid())
    print("bridge up", flush=True)
    code = home / "data" / "bridge-exit"
    if code.exists():
        sys.exit(int(code.read_text()))
    time.sleep(600)
"""
_SLEEP = "import time; time.sleep(600)"


def _wait_for(predicate, timeout=10.0, step=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if step:
            step()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _starts(home):
    try:
        return (pathlib.Path(home) / "data" / "bridge-starts").read_text().split()
    except OSError:
        return []


def _bridge_argv(home):
    return [sys.executable, "-c", textwrap.dedent(_BRIDGE), str(home)]


def _cousin(root, slug, *, runner="fake", telegram=None):
    """A cousin home. `telegram`: None (no table), or a dict with
    `enabled` and `operators` (a token file is written with it)."""
    home = pathlib.Path(root) / "cousins" / slug
    for sub in ("data", "run", "memory"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    _write(root, home, slug, runner, telegram)
    return home


def _write(root, home, slug, runner="fake", telegram=None):
    text = '[cousin]\nslug = "%s"\nname = "%s"\n' % (slug, slug.capitalize())
    if runner is not None:
        text += '\n[agent]\nrunner = "%s"\n' % runner
    if telegram is not None:
        token = pathlib.Path(root) / "config" / "telegram" / ("%s.token" % slug)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(TOKEN)
        ops = ", ".join('{ user_id = %d, name = "Ana" }' % i
                        for i in telegram.get("operators", [42]))
        text += ('\n[telegram]\nenabled = %s\ntoken_file = "config/telegram/%s.token"\n'
                 'operators = [%s]\n'
                 % ("true" if telegram.get("enabled", True) else "false", slug, ops))
    (pathlib.Path(home) / "cousin.toml").write_text(text)


def _runner(home):
    """A runner child for `home` that only sleeps (no real runner)."""
    slug = pathlib.Path(home).name
    return ChildSpec("runner:%s" % slug, "runner", [sys.executable, "-c", _SLEEP],
                     stop_timeout=5.0, slug=slug, home=home)


class _Case(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.root = self.dir / "root"
        (self.root / "config").mkdir(parents=True)
        self.argv_patch = mock.patch.object(supervisor, "_bridge_argv", _bridge_argv)
        self.argv_patch.start()
        self.addCleanup(self.argv_patch.stop)

    def supervise(self, specs, **kw):
        opts = dict(FAST)
        opts.update(kw)
        out = io.StringIO()
        sup = Supervisor(self.root, specs, out=out, **opts)
        self.addCleanup(self._kill_all, sup)
        sup.start_all()
        return sup, out

    def _kill_all(self, sup):
        for child in list(sup.children.values()):
            if child.proc is not None and child.proc.returncode is None:
                try:
                    os.killpg(child.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        sup.stop_all()

    def row(self, sup, name):
        return sup.status()["children"].get(name)


class TestTheChild(_Case):
    def test_the_bridge_is_the_entry_point_the_tmux_lane_starts(self):
        home = self.root / "cousins" / "wren"
        spec = supervisor.telegram_spec(home)
        self.assertEqual((spec.name, spec.kind, spec.slug), ("telegram:wren", "telegram", "wren"))
        self.argv_patch.stop()
        try:
            argv = supervisor.telegram_spec(home).argv
        finally:
            self.argv_patch.start()
        self.assertEqual(argv, [sys.executable, "-m", "cousin_lib.telegram", "--home", str(home)])

    def test_an_enabled_runner_cousin_gets_a_telegram_child_after_its_runner(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, out = self.supervise([_runner(home)])
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 1, step=sup.step))
        row = self.row(sup, "telegram:wren")
        self.assertEqual(row["state"], "running")
        self.assertEqual(_starts(home), [str(row["pid"])])
        # the pid file the console and telegram_admin read
        self.assertEqual((home / "data" / "telegram.pid").read_text().strip(), str(row["pid"]))
        lines = out.getvalue()
        self.assertLess(lines.index("started runner:wren"), lines.index("started telegram:wren"))
        # its output goes to the supervisor's and to data/telegram.log
        self.assertTrue(_wait_for(lambda: "telegram:wren | bridge up" in out.getvalue(),
                                  step=sup.step))
        self.assertIn("bridge up", (home / "data" / "telegram.log").read_text())
        # status lists it like the other children
        self.assertEqual(sorted(sup.status()["children"]), ["runner:wren", "telegram:wren"])

    def test_a_disabled_or_invalid_config_gets_none_and_says_why(self):
        wren = _cousin(self.root, "wren", telegram={"enabled": False})
        sam = _cousin(self.root, "sam", telegram={"enabled": True, "operators": []})
        toki = _cousin(self.root, "toki")                           # no [telegram] at all
        sup, out = self.supervise([_runner(wren), _runner(sam), _runner(toki)])
        _wait_for(lambda: False, timeout=0.3, step=sup.step)
        self.assertEqual(sorted(sup.status()["children"]),
                         ["runner:sam", "runner:toki", "runner:wren"])
        lines = out.getvalue()
        self.assertIn("telegram:wren not started: [telegram] enabled is not true", lines)
        self.assertIn("telegram:sam not started: no operators configured", lines)
        self.assertNotIn("telegram:toki", lines)
        self.assertEqual(_starts(sam), [])

    def test_a_tmux_cousin_gets_no_supervisor_bridge(self):
        _cousin(self.root, "priya", runner=None, telegram={"enabled": True})
        wren = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, _ = self.supervise([])
        with mock.patch.object(supervisor, "runner_spec", _runner):
            added, _ = sup.reload()
        self.assertEqual(sorted(added), ["runner:wren", "telegram:wren"])
        self.assertNotIn("telegram:priya", sup.status()["children"])
        self.assertTrue(_wait_for(lambda: len(_starts(wren)) == 1, step=sup.step))
        self.assertEqual(_starts(self.root / "cousins" / "priya"), [])


class TestLifecycle(_Case):
    def test_the_bridge_stops_and_is_held_with_its_runner(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, _ = self.supervise([_runner(home)])
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 1, step=sup.step))
        first = self.row(sup, "telegram:wren")["pid"]
        sup.stop_child("runner:wren", wait=False, by="Testa")
        self.assertTrue(_wait_for(lambda: self.row(sup, "telegram:wren")["state"] == "stopped",
                                  step=sup.step))
        self.assertTrue(supervisor.is_held(home))
        _wait_for(lambda: False, timeout=0.5, step=sup.step)       # past the backoff
        row = self.row(sup, "telegram:wren")
        self.assertEqual((row["state"], row["pid"]), ("stopped", None))
        self.assertEqual(len(_starts(home)), 1)
        self.assertFalse((home / "data" / "telegram.pid").exists())
        # a reload while held leaves it down
        sup.reload()
        self.assertEqual(self.row(sup, "telegram:wren")["state"], "stopped")
        # start of the runner starts its bridge again
        self.assertTrue(sup.start_child("runner:wren")["ok"])
        row = self.row(sup, "telegram:wren")
        self.assertEqual(row["state"], "running")
        self.assertNotEqual(row["pid"], first)

    def test_the_ordered_stop_takes_the_bridge_down_before_its_runner(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, out = self.supervise([_runner(home)])
        sup.stop_all()
        lines = out.getvalue()
        self.assertLess(lines.index("telegram:wren stopped"), lines.index("runner:wren stopped"))

    def test_a_crashed_bridge_is_restarted_with_the_backoff(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        (home / "data" / "bridge-exit").write_text("1")
        sup, out = self.supervise([_runner(home)], max_exits=100)
        self.assertTrue(_wait_for(lambda: len(_starts(home)) >= 3, step=sup.step))
        row = self.row(sup, "telegram:wren")
        self.assertGreaterEqual(row["restarts"], 2)
        self.assertEqual(row["last_exit"], "code 1")
        self.assertIn("telegram:wren exited (code 1), restarting in", out.getvalue())

    def test_a_configuration_exit_is_failing_never_a_crash_loop(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        (home / "data" / "bridge-exit").write_text("2")
        sup, _ = self.supervise([_runner(home)])
        self.assertTrue(_wait_for(lambda: self.row(sup, "telegram:wren")["state"] == "failing",
                                  step=sup.step))
        _wait_for(lambda: False, timeout=0.5, step=sup.step)
        self.assertEqual(len(_starts(home)), 1)


class TestRescan(_Case):
    def test_a_rescan_adds_and_removes_the_bridge(self):
        home = _cousin(self.root, "wren", telegram={"enabled": False})
        sup, _ = self.supervise([_runner(home)])
        self.assertIsNone(self.row(sup, "telegram:wren"))
        _write(self.root, home, "wren", telegram={"enabled": True})
        added, removed = sup.reload()
        self.assertEqual((added, removed), (["telegram:wren"], []))
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 1, step=sup.step))
        pid = self.row(sup, "telegram:wren")["pid"]
        runner_pid = self.row(sup, "runner:wren")["pid"]
        _write(self.root, home, "wren", telegram={"enabled": False})
        added, removed = sup.reload()
        self.assertEqual((added, removed), ([], ["telegram:wren"]))
        self.assertTrue(_wait_for(lambda: self.row(sup, "telegram:wren") is None, step=sup.step))
        self.assertFalse(os.path.exists("/proc/%d" % pid))              # stopped and reaped
        self.assertEqual(self.row(sup, "runner:wren")["pid"], runner_pid)    # not bounced

    def test_a_rescan_restarts_the_bridge_on_a_changed_config_only(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, _ = self.supervise([_runner(home)])
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 1, step=sup.step))
        first = self.row(sup, "telegram:wren")["pid"]
        sup.reload()                                               # nothing changed
        self.assertEqual(self.row(sup, "telegram:wren")["pid"], first)
        _write(self.root, home, "wren", telegram={"enabled": True, "operators": [42, 43]})
        sup.reload()
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 2, step=sup.step))
        self.assertNotEqual(self.row(sup, "telegram:wren")["pid"], first)
        self.assertEqual(self.row(sup, "telegram:wren")["restarts"], 0)   # not a crash

    def test_a_disable_then_enable_before_the_exit_is_reaped_keeps_the_bridge(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, _ = self.supervise([_runner(home)])
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 1, step=sup.step))
        first = self.row(sup, "telegram:wren")["pid"]
        _write(self.root, home, "wren", telegram={"enabled": False})
        sup.reload()                                   # SIGTERM, not reaped yet
        _write(self.root, home, "wren", telegram={"enabled": True})
        sup.reload()                                   # before any step() reaps it
        # no further reload: once the exit is reaped the bridge runs again
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 2, step=sup.step))
        _wait_for(lambda: False, timeout=0.3, step=sup.step)
        row = self.row(sup, "telegram:wren")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "running")
        self.assertNotEqual(row["pid"], first)

    def test_a_cousin_leaving_the_runner_lane_takes_its_bridge(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, _ = self.supervise([_runner(home)])
        _write(self.root, home, "wren", runner=None, telegram={"enabled": True})
        _, removed = sup.reload()
        self.assertEqual(sorted(removed), ["runner:wren", "telegram:wren"])
        self.assertTrue(_wait_for(lambda: sup.status()["children"] == {}, step=sup.step))


class TestNoDoubleBridge(_Case):
    def test_a_bridge_running_outside_the_supervisor_is_left_alone(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        # a bridge started by hand, before this fix: its cmdline carries the marker
        outside = subprocess.Popen([sys.executable, "-c", _SLEEP, "cousin_lib.telegram"],
                                   start_new_session=True)
        self.addCleanup(lambda: outside.poll() is None and outside.kill())
        (home / "data" / "telegram.pid").write_text("%d\n" % outside.pid)
        sup, out = self.supervise([_runner(home)])
        _wait_for(lambda: False, timeout=0.5, step=sup.step)
        self.assertEqual(_starts(home), [])
        row = self.row(sup, "telegram:wren")
        self.assertEqual(row["pid"], None)
        self.assertIn("outside the supervisor", row["reason"])
        self.assertEqual(out.getvalue().count("a bridge outside the supervisor runs (pid %d)"
                                              % outside.pid), 1)
        self.assertTrue(outside.poll() is None)                    # left alone
        outside.kill()
        outside.wait(5)
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 1, step=sup.step))
        self.assertEqual(self.row(sup, "telegram:wren")["state"], "running")

    def test_a_disable_stops_a_bridge_running_outside_the_supervisor(self):
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        outside = subprocess.Popen([sys.executable, "-c", _SLEEP, "cousin_lib.telegram"],
                                   start_new_session=True)
        self.addCleanup(lambda: outside.poll() is None and outside.kill())
        (home / "data" / "telegram.pid").write_text("%d\n" % outside.pid)
        sup, out = self.supervise([_runner(home)])
        self.assertEqual(self.row(sup, "telegram:wren")["state"], "backoff")
        _write(self.root, home, "wren", telegram={"enabled": False})
        sup.reload()
        self.assertTrue(_wait_for(lambda: outside.poll() is not None, timeout=5.0))
        self.assertIsNone(self.row(sup, "telegram:wren"))
        self.assertIn("stopping the bridge outside the supervisor (pid %d)" % outside.pid,
                      out.getvalue())
        self.assertEqual(_starts(home), [])

    def test_a_disabled_cousin_never_stops_a_bridge_the_supervisor_runs(self):
        # the pid file names our own child: it is ours to stop, once
        home = _cousin(self.root, "wren", telegram={"enabled": True})
        sup, out = self.supervise([_runner(home)])
        self.assertTrue(_wait_for(lambda: len(_starts(home)) == 1, step=sup.step))
        _write(self.root, home, "wren", telegram={"enabled": False})
        sup.reload()
        self.assertNotIn("outside the supervisor", out.getvalue())
        self.assertTrue(_wait_for(lambda: self.row(sup, "telegram:wren") is None, step=sup.step))


if __name__ == "__main__":
    unittest.main()
