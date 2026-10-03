"""cousin-supervisor's control surface: the unix socket, the CLI, the
state snapshot, one supervisor per root, and the rescan on SIGHUP or
`reload`. The supervisor runs as a real process
(`python3 -m cousin_lib.supervisor run --no-console --no-loops`) over
`fake` runner cousins. Invented cast only; every wait has a deadline."""
import contextlib
import fcntl
import io
import json
import os
import pathlib
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from cousin_lib import supervisor
from cousin_lib.runner.main import is_running
from cousin_lib.supervisor import SupervisorUnavailable, request, runner_cousins, snapshot
from tests._hermetic import HermeticCase


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _cousin(root, slug, runner=None, auto_start=None):
    """A cousin home; `runner=None` is a tmux cousin (no [agent] runner)."""
    home = pathlib.Path(root) / "cousins" / slug
    for sub in ("data", "run", "memory"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    text = '[cousin]\nslug = "%s"\nname = "%s"\n' % (slug, slug.capitalize())
    if runner is not None:
        text += '\n[agent]\nrunner = "%s"\n' % runner
        if auto_start is not None:
            text += "auto_start = %s\n" % ("true" if auto_start else "false")
    (home / "cousin.toml").write_text(text)
    return home


class _Case(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.root = self.dir / "root"
        (self.root / "config").mkdir(parents=True)
        self.pids = set()           # every child pid seen, killed in cleanup

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = supervisor.supervisor_main(list(argv) + ["--root", str(self.root)])
        return rc, out.getvalue(), err.getvalue()

    def supervise(self):
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
            for pid in self.pids:
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)
        self.addCleanup(_cleanup)
        self.assertTrue(_wait_for(self.status), "the supervisor never answered:\n%s" % self.log())
        return proc

    def log(self):
        return (self.dir / "supervisor.log").read_text()

    def status(self):
        try:
            body = request(self.root, "status", timeout=5.0)
        except SupervisorUnavailable:
            return None
        for row in body["children"].values():
            if row["pid"]:
                self.pids.add(row["pid"])
        return body

    def child(self, name):
        return (self.status() or {}).get("children", {}).get(name)

    def wait_state(self, name, state, timeout=15.0):
        row = _wait_for(lambda: (self.child(name) or {}).get("state") == state
                        and self.child(name), timeout)
        self.assertTrue(row, "%s never reached %s: %s\n%s"
                        % (name, state, self.child(name), self.log()))
        return row


class TestSocket(_Case):
    def test_status_over_the_socket_lists_every_child(self):
        _cousin(self.root, "wren", "fake")
        _cousin(self.root, "sam", "fake")
        _cousin(self.root, "priya")                     # tmux: never the supervisor's
        proc = self.supervise()
        self.wait_state("runner:sam", "running")
        body = self.status()
        self.assertEqual((body["ok"], body["pid"]), (True, proc.pid))
        self.assertEqual(sorted(body["children"]), ["runner:sam", "runner:wren"])
        for row in body["children"].values():
            self.assertEqual(sorted(row), ["last_exit", "pid", "reason", "restarts",
                                           "since", "state"])
        self.assertTrue(_wait_for(lambda: is_running(self.root / "cousins" / "wren")))
        rc, out, _ = self.cli("status", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(json.loads(out)["children"]), ["runner:sam", "runner:wren"])
        rc, out, _ = self.cli("status")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"runner:wren\s+running")
        self.assertEqual(request(self.root, "bogus")["ok"], False)

    def test_start_and_stop_by_slug(self):
        _cousin(self.root, "wren", "fake")
        _cousin(self.root, "toki", "fake", auto_start=False)
        _cousin(self.root, "priya")
        self.supervise()
        self.wait_state("runner:wren", "running")
        self.assertIsNone(self.child("runner:toki"))    # auto_start = false
        reply = request(self.root, "start", slug="toki")
        self.assertEqual(reply, {"ok": True, "name": "runner:toki", "state": "running"})
        self.assertTrue(_wait_for(lambda: is_running(self.root / "cousins" / "toki")))
        wren = self.child("runner:wren")["pid"]
        rc, out, _ = self.cli("stop", "wren")
        self.assertEqual((rc, out.strip()), (0, "runner:wren stopped"))
        self.assertFalse(_alive(wren))
        self.assertEqual(self.child("runner:wren")["reason"], "stopped by request")
        rc, _, err = self.cli("start", "priya")         # no runner: refused by name
        self.assertEqual(rc, 2)
        from cousin_lib.delivery import lane_refusal
        self.assertIn(lane_refusal(self.root / "cousins" / "priya"), err)
        rc, _, err = self.cli("start", "mallory")       # no such cousin
        self.assertEqual(rc, 2)
        self.assertIn("mallory is not a cousin", err)

    def test_stop_without_wait_answers_at_once(self):
        # `wait: false` is answered once signalled; the table (and the
        # console's fleet row) shows when it is down
        _cousin(self.root, "wren", "fake")
        self.supervise()
        pid = self.wait_state("runner:wren", "running")["pid"]
        self.assertEqual(request(self.root, "stop", slug="wren", wait=False, timeout=5),
                         {"ok": True, "name": "runner:wren", "state": "stopping"})
        self.wait_state("runner:wren", "stopped")
        self.assertFalse(_alive(pid))
        self.assertEqual(request(self.root, "stop", slug="wren", wait="no")["error"],
                         "wait must be true or false")

    def test_a_request_names_a_slug_or_a_name_never_both(self):
        _cousin(self.root, "wren", "fake")
        self.supervise()
        self.wait_state("runner:wren", "running")
        for args in ({}, {"slug": "wren", "name": "loops"}):
            self.assertIn("exactly one", request(self.root, "stop", **args)["error"])
        self.assertIn("addressed by slug",
                      request(self.root, "stop", name="runner:wren")["error"])
        self.assertEqual(request(self.root, "stop", name="loops")["error"],
                         "no child named loops")                  # --no-loops
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("start")                                     # neither slug nor --name
        self.assertEqual(self.child("runner:wren")["state"], "running")

    def test_stop_holds_until_start(self):
        _cousin(self.root, "wren", "fake")
        self.supervise()
        first = self.wait_state("runner:wren", "running")["pid"]
        self.assertEqual(request(self.root, "stop", slug="wren", timeout=60),
                         {"ok": True, "name": "runner:wren", "state": "stopped"})
        time.sleep(2.5)                                  # past the first backoff delays
        row = self.child("runner:wren")
        self.assertEqual((row["state"], row["pid"]), ("stopped", None))
        rc, out, _ = self.cli("start", "wren")
        self.assertEqual((rc, out.strip()), (0, "runner:wren running"))
        again = self.wait_state("runner:wren", "running")["pid"]
        self.assertNotEqual(again, first)

    def test_a_stop_is_held_across_a_supervisor_restart(self):
        # the stop writes <home>/run/held; a new supervisor does not
        # start a held cousin; `start` removes the marker
        wren = _cousin(self.root, "wren", "fake")
        _cousin(self.root, "sam", "fake")
        proc = self.supervise()
        self.wait_state("runner:wren", "running")
        rc, out, _ = self.cli("stop", "wren")
        self.assertEqual((rc, out.strip()), (0, "runner:wren stopped"))
        stamp, who = (wren / "run" / "held").read_text().rstrip("\n").split(" ", 1)
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(who, "cousin-supervisor stop")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(40), 0)
        self.supervise()                                          # a new supervisor
        self.wait_state("runner:sam", "running")
        self.assertIsNone(self.child("runner:wren"))              # held: not started
        self.assertEqual([c.slug for c in runner_cousins(self.root)], ["sam"])
        # a stop of a held runner cousin with no child is still an ok stop
        self.assertEqual(request(self.root, "stop", slug="wren", by="Testa"),
                         {"ok": True, "name": "runner:wren", "state": "stopped"})
        self.assertTrue((wren / "run" / "held").read_text().endswith(" Testa\n"))
        self.assertEqual(request(self.root, "start", slug="wren"),
                         {"ok": True, "name": "runner:wren", "state": "running"})
        self.assertFalse((wren / "run" / "held").exists())
        self.assertTrue(_wait_for(lambda: is_running(wren)))

    def test_request_without_a_supervisor_raises_unavailable(self):
        with self.assertRaises(SupervisorUnavailable):
            request(self.root, "status")
        run = self.root / "run"
        run.mkdir()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.root / supervisor.SOCKET))    # a socket file nobody listens on
        stale.close()
        with self.assertRaises(SupervisorUnavailable):
            request(self.root, "status")

    def test_a_slow_supervisor_is_unavailable_but_not_absent(self):
        """A caller whose fallback does the supervisor's work itself
        must run it only when there is no supervisor (nothing to connect
        to), never for one that is alive but slower than the timeout."""
        with self.assertRaises(supervisor.SupervisorAbsent):
            request(self.root, "status")                      # no socket
        run = self.root / "run"
        run.mkdir(exist_ok=True)
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        slow.bind(str(self.root / supervisor.SOCKET))
        slow.listen(1)                                         # accepts, never answers
        self.addCleanup(slow.close)
        with self.assertRaises(SupervisorUnavailable) as cm:
            request(self.root, "status", timeout=0.2)
        self.assertNotIsInstance(cm.exception, supervisor.SupervisorAbsent)
        self.assertIn("did not answer", str(cm.exception))

    def test_cli_status_exits_1_with_no_supervisor(self):
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 1)
        self.assertIn("no cousin-supervisor", err)
        for argv in (("start", "wren"), ("stop", "wren"), ("reload",)):
            self.assertEqual(self.cli(*argv)[0], 1)

    def test_run_socket_dir_is_private(self):
        _cousin(self.root, "wren", "fake")
        (self.root / "run").mkdir(mode=0o755)
        self.supervise()
        self.assertEqual(stat.S_IMODE((self.root / "run").stat().st_mode), 0o700)
        self.assertTrue(stat.S_ISSOCK((self.root / supervisor.SOCKET).stat().st_mode))


class TestRefusedAndRemoved(_Case):
    """`status` lists every cousin it refuses (no runner kind; a
    worker is not one) with the one line, and every removed key it finds
    in a cousin or the install, named, not fatal."""

    def test_status_lists_a_refused_cousin(self):
        from cousin_lib.delivery import lane_refusal
        _cousin(self.root, "wren", "fake")
        priya = _cousin(self.root, "priya")                     # no [agent] runner
        toki = _cousin(self.root, "toki")
        (toki / "cousin.toml").write_text('[cousin]\nslug = "toki"\ntype = "worker"\n')
        self.supervise()
        self.wait_state("runner:wren", "running")
        body = self.status()
        self.assertEqual(body["refused"], {"priya": lane_refusal(priya)})
        rc, out, _ = self.cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("refused  priya: %s" % lane_refusal(priya), out)
        self.assertNotIn("toki", out)
        self.assertEqual(json.loads(self.cli("status", "--json")[1])["refused"],
                         {"priya": lane_refusal(priya)})

    def test_status_lists_the_removed_keys(self):
        wren = _cousin(self.root, "wren", "fake")
        (wren / "cousin.toml").write_text((wren / "cousin.toml").read_text()
                                          + '\n[chat]\nport = 8091\n')
        (self.root / "config" / "harness.toml").write_text('busy_patterns = ["esc"]\n')
        self.supervise()
        self.wait_state("runner:wren", "running")              # named, and it runs
        body = self.status()
        self.assertEqual([(c["cousin"], c["where"], c["key"]) for c in body["config"]],
                         [("wren", "cousin.toml", "[chat] port"),
                          (None, "config/harness.toml", "busy_patterns")])
        self.assertTrue(all(c["line"] for c in body["config"]))
        self.assertEqual(body["refused"], {})
        rc, out, _ = self.cli("status")
        self.assertIn("removed  wren cousin.toml [chat] port", out)
        self.assertIn("removed  config/harness.toml busy_patterns", out)
        self.assertIn("cousin-migrate tidy --all", out)


class TestTargets(_Case):
    def test_a_cousin_slugged_loops_is_never_the_loops_daemon(self):
        # `slug` is only ever runner:<slug>, `name` only console or loops
        home = _cousin(self.root, "loops", "fake")
        sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
        sup = supervisor.Supervisor(
            self.root, [supervisor.ChildSpec("loops", "loops", sleep, stop_timeout=5.0),
                        supervisor.ChildSpec("runner:loops", "runner", sleep,
                                             stop_timeout=5.0, slug="loops")],
            out=io.StringIO(), tick=0.02)
        self.addCleanup(sup.stop_all)
        sup.start_all()
        self.assertEqual(sup._handle({"op": "stop", "slug": "loops", "wait": False,
                                      "by": "Wren"}, None),
                         {"ok": True, "name": "runner:loops", "state": "stopping"})
        daemon = sup.children["loops"]
        self.assertTrue(daemon.alive)
        self.assertFalse(daemon.stopping)
        self.assertTrue(supervisor.is_held(home))
        self.assertEqual(sup._handle({"op": "stop", "name": "loops", "wait": False}, None),
                         {"ok": True, "name": "loops", "state": "stopping"})
        self.assertFalse((self.root / "run" / "held").exists())   # no marker for a daemon


class TestOnePerRoot(_Case):
    def test_a_second_supervisor_on_the_same_root_exits_2(self):
        _cousin(self.root, "wren", "fake")
        first = self.supervise()
        second = subprocess.run([sys.executable, "-m", "cousin_lib.supervisor", "run",
                                 "--root", str(self.root), "--no-console", "--no-loops"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 2)
        self.assertIn("another cousin-supervisor holds", second.stderr)
        self.assertEqual(self.status()["pid"], first.pid)        # the first is untouched


class TestSnapshot(_Case):
    def test_snapshot_is_written_and_read_back_while_alive(self):
        _cousin(self.root, "wren", "fake")
        proc = self.supervise()
        self.wait_state("runner:wren", "running")
        body = _wait_for(lambda: (snapshot(self.root) or {}).get("children", {})
                         .get("runner:wren", {}).get("state") == "running"
                         and snapshot(self.root))
        self.assertTrue(body)
        self.assertEqual(body["pid"], proc.pid)
        self.assertEqual(body["children"]["runner:wren"]["pid"],
                         self.child("runner:wren")["pid"])
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(40), 0)
        self.assertIsNone(snapshot(self.root))

    def test_a_stale_snapshot_reads_as_none(self):
        # liveness is the lock, not the pid: a live pid (a reused one, a
        # PID 1 of an earlier container) with nobody holding the lock is stale
        self.assertIsNone(snapshot(self.root))              # missing
        path = self.root / supervisor.SNAPSHOT
        path.parent.mkdir()
        path.write_text(json.dumps({"ok": True, "pid": os.getpid(), "children": {}}))
        self.assertIsNone(snapshot(self.root))              # no lock file
        (self.root / supervisor.LOCK).write_text("")
        self.assertIsNone(snapshot(self.root))              # a lock file nobody holds
        fd = os.open(self.root / supervisor.LOCK, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)                      # what a live supervisor holds
        self.assertEqual(snapshot(self.root)["pid"], os.getpid())
        path.write_text("{not json")
        self.assertIsNone(snapshot(self.root))
        path.write_text("[1, 2]")
        self.assertIsNone(snapshot(self.root))

    def test_a_snapshot_probe_never_reads_as_a_second_supervisor(self):
        # the probe takes the lock shared for a moment; a supervisor starting
        # meanwhile retries instead of exiting 2 (LOCK_TAKE_S)
        (self.root / "run").mkdir()
        lock = self.root / supervisor.LOCK
        lock.write_text("")
        holder = textwrap.dedent('''
            import fcntl, os, sys, time
            fd = os.open(sys.argv[1], os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_SH)
            print("held", flush=True)
            time.sleep(0.3)
        ''')
        with subprocess.Popen([sys.executable, "-c", holder, str(lock)],
                              stdout=subprocess.PIPE) as reader:
            self.addCleanup(reader.kill)
            self.assertEqual(reader.stdout.readline(), b"held\n")
            sup = supervisor.Supervisor(self.root, [])
            fd = sup._take_lock()                            # waits the reader out
            os.close(fd)
            reader.wait(10)


class TestStartClearsTheGiveUp(_Case):
    def test_an_explicit_start_clears_a_tmux_give_up_like_the_hold(self):
        home = _cousin(self.root, "wren", "tmux")
        (home / "data" / "run").mkdir(parents=True, exist_ok=True)
        marker = home / "data" / "run" / "tmux-giving-up.json"
        marker.write_text('{"reason": "x", "at": 1}')
        supervisor.hold(home, "Priya")
        sup = supervisor.Supervisor(self.root, [])
        sup._start = lambda child: None
        sup._sync_bridges = lambda *a, **k: None
        sup.start_child("runner:wren")
        self.assertFalse(supervisor.is_held(home))
        self.assertFalse(marker.exists())


class TestRescan(_Case):
    def test_runner_cousins_honours_auto_start_false_and_skips_tmux_cousins(self):
        _cousin(self.root, "wren", "fake")
        _cousin(self.root, "sam", "sdk", auto_start=True)
        _cousin(self.root, "toki", "fake", auto_start=False)
        _cousin(self.root, "priya")                                  # tmux
        _cousin(self.root, "testa", "bogus")                         # not a runner kind
        broken = _cousin(self.root, "mallory", "fake")
        (broken / "cousin.toml").write_text("[agent\n")              # does not parse
        self.assertEqual([c.slug for c in runner_cousins(self.root)], ["sam", "wren"])
        self.assertEqual(runner_cousins(self.dir / "nowhere"), [])
        supervisor.hold(self.root / "cousins" / "sam", "Priya")        # held is skipped
        self.assertEqual([c.slug for c in runner_cousins(self.root)], ["wren"])
        supervisor.release(self.root / "cousins" / "sam")
        self.assertEqual([c.slug for c in runner_cousins(self.root)], ["sam", "wren"])

    def test_sighup_picks_up_a_new_cousin_without_bouncing_the_others(self):
        _cousin(self.root, "wren", "fake")
        proc = self.supervise()
        wren = self.wait_state("runner:wren", "running")["pid"]
        _cousin(self.root, "sam", "fake")
        proc.send_signal(signal.SIGHUP)
        self.wait_state("runner:sam", "running")
        row = self.child("runner:wren")
        self.assertEqual((row["pid"], row["restarts"]), (wren, 0))
        self.assertIn("supervisor: reload: added runner:sam", self.log())

    def test_a_burst_of_signals_never_hangs_the_supervisor(self):
        # a handler runs on the main thread between two of its bytecodes;
        # one that took the loop's wake lock while the loop held it (inside
        # its wait or clear) waited for itself forever, and the SIGTERM
        # after it never stopped the supervisor. A burst lands in that
        # window every time; a single SIGTERM only now and then.
        proc = self.supervise()
        for _ in range(2000):
            proc.send_signal(signal.SIGHUP)
            time.sleep(0.0005)
        self.assertTrue(_wait_for(self.status, timeout=10),
                        "the supervisor stopped answering:\n%s" % self.log())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(20), 0)

    def test_reload_clears_failing_and_removes_a_cousin_that_left_the_lane(self):
        _cousin(self.root, "wren", "fake")
        sam = _cousin(self.root, "sam", "fake")
        (sam / "policy.toml").write_text('deny_tools = "x"\n')         # the runner exits 2
        self.supervise()
        wren = self.wait_state("runner:wren", "running")["pid"]
        self.assertEqual(self.wait_state("runner:sam", "failing")["reason"],
                         "configuration (exit 2)")
        (sam / "policy.toml").unlink()                                # fixed
        _cousin(self.root, "wren")                                    # now a tmux cousin
        rc, out, _ = self.cli("reload")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "added: -; removed: runner:wren")
        self.wait_state("runner:sam", "running")
        self.assertTrue(_wait_for(lambda: self.child("runner:wren") is None))
        self.assertFalse(_alive(wren))


if __name__ == "__main__":
    unittest.main()
