"""The clock: the supervisor owns schedules and loops
by running the loops daemon as its child; one process is the clock,
never each runner. A real `cousin-supervisor run` over one `fake` runner
cousin: a one-shot and a `[[loops]]` entry land in the runner's inbox on
time, with only the supervisor and its two children running. Invented
cast only; every wait has a deadline and every process is killed in
cleanup. A stray loops daemon beside it is refused (exit 5), and one that
held the lock first is waited for: the supervisor's loops child stays in
`backoff`, never `failing`, and ticks once the stray is gone."""
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from tests._hermetic import HermeticCase

ONE_SHOT = "Wren, call Testa back about the seedlings."


def _is_shot(body):
    # the provenance prefix, the job's header line, then the prompt verbatim
    return body.startswith("[cousin-schedule] #") and body.endswith("\n\n" + ONE_SHOT)


LOOP_PROMPT = "Wren, water the tomatoes."
DEADLINE_S = 20.0

REPO = pathlib.Path(__file__).resolve().parent.parent


def _wait_for(predicate, timeout=DEADLINE_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _children(pid):
    """{child pid: argv} of `pid`'s direct children, from /proc/*/stat
    (the ppid is the second field after the command's closing paren)."""
    found = {}
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue                    # exited while we looked
        if int(stat.rsplit(")", 1)[1].split()[1]) == pid:
            found[int(entry.name)] = [a.decode() for a in argv if a]
    return found


def _rows(db, sql):
    """Rows from a store someone else writes; [] until it exists."""
    if not pathlib.Path(db).exists():
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return []                       # the table is not created yet


class _ClockCase(HermeticCase):
    """A root with one `fake` runner cousin, Wren, whose one `[[loops]]`
    entry is due at once; supervise() starts a real supervisor on it."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.root = self.dir / "root"
        (self.root / "config").mkdir(parents=True)
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "run", "memory"):
            (self.home / sub).mkdir(parents=True)
        # a runner cousin with no beat and no daily flip: the only things
        # due are the loop below and the one-shot added next
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n'
            '[agent]\nrunner = "fake"\n\n'
            '[heartbeat]\ncontext_beat_seconds = 0\n\n'
            '[lifecycle]\nflip_at = "never"\n\n'
            '[[loops]]\nname = "water"\ninterval_seconds = 3600\n'
            'prompt = "%s"\n' % LOOP_PROMPT)
        self.inbox_db = self.home / "data" / "inbox.db"
        self.sched_db = self.root / "data" / "scheduled.db"

    def supervise(self):
        log = open(self.dir / "supervisor.log", "w")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-m", "cousin_lib.supervisor", "run",
                                 "--root", str(self.root), "--no-console",
                                 "--loops-interval", "1"],
                                cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        seen = set()

        def _cleanup():
            if proc.poll() is None:
                seen.update(_children(proc.pid))
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(45)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(5)
            for pid in seen:
                if _alive(pid):
                    try:
                        os.killpg(pid, signal.SIGKILL)   # each child leads its own session
                    except OSError:
                        pass
        self.addCleanup(_cleanup)
        self.seen = seen
        return proc

    def log(self):
        return (self.dir / "supervisor.log").read_text()

    def inbox(self):
        return _rows(self.inbox_db, "SELECT thread_id, source, body, created_at FROM inbox"
                                    " ORDER BY id")


@unittest.skipUnless(os.path.isdir("/proc/self"), "reads the process table from /proc")
class TestTheClock(_ClockCase):
    def setUp(self):
        super().setUp()
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root), COUSIN_HOME=str(self.home))
        added = subprocess.run([sys.executable, "-m", "cousin_lib.schedule", "add",
                                "in 3s", ONE_SHOT], env=env, cwd=REPO,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.started = time.time()
        self.proc = self.supervise()

    def both_landed(self):
        if self.proc.poll() is not None:
            self.fail("the supervisor exited with %s:\n%s" % (self.proc.returncode, self.log()))
        bodies = [r[2] for r in self.inbox()]
        return any(_is_shot(b) for b in bodies) and LOOP_PROMPT in bodies

    def test_a_one_shot_and_a_loop_land_in_the_runner_inbox_on_schedule(self):
        self.assertTrue(_wait_for(self.both_landed),
                        "inbox: %r\n%s" % (self.inbox(), self.log()))
        jobs = "SELECT status, target_ts, fired_at FROM scheduled_jobs"
        # the store is marked just after the inbox put returns (schedule.tick)
        self.assertTrue(_wait_for(lambda: _rows(self.sched_db, jobs)[0][0] == "fired", 5),
                        "%r\n%s" % (_rows(self.sched_db, jobs), self.log()))
        (job,) = _rows(self.sched_db, jobs)
        status, target, fired_at = job
        # on time: not before its target, and within a few ticks after it
        self.assertGreaterEqual(fired_at, target)
        self.assertLessEqual(fired_at, target + 10)
        time.sleep(3)                   # three more ticks: nothing fires twice
        rows = self.inbox()
        shots = [r for r in rows if _is_shot(r[2])]
        loops = [r for r in rows if r[2] == LOOP_PROMPT]
        self.assertEqual(len(shots), 1, rows)
        self.assertEqual(len(loops), 1, rows)
        self.assertEqual(len(rows), 2, "nothing else is due (no beat, no flip): %r" % rows)
        # the daemon's delivery shape: thread loop:daemon, source loop
        for thread_id, source, _body, created_at in shots + loops:
            self.assertEqual((thread_id, source), ("loop:daemon", "loop"))
            self.assertGreaterEqual(created_at, self.started)
        self.assertGreaterEqual(shots[0][3], target)
        self.assertEqual(_rows(self.sched_db, "SELECT count(*) FROM scheduled_jobs"
                                              " WHERE status = 'fired'"), [(1,)])

    def test_no_per_cousin_timer_process(self):
        self.assertTrue(_wait_for(self.both_landed),
                        "inbox: %r\n%s" % (self.inbox(), self.log()))
        children = _children(self.proc.pid)
        self.seen.update(children)
        shapes = sorted(" ".join(argv[1:4]) for argv in children.values())
        self.assertEqual(shapes, ["-m cousin_lib.loops run",
                                  "-m cousin_lib.runner.main --home"],
                         "the supervisor's children: %r" % children)
        runner = [argv for argv in children.values() if "cousin_lib.runner.main" in argv]
        self.assertEqual(runner[0][-1], str(self.home))
        # and neither was restarted into place: one start line each
        log = self.log()
        self.assertEqual(len(re.findall(r"started loops \(pid", log)), 1, log)
        self.assertEqual(len(re.findall(r"started runner:wren \(pid", log)), 1, log)

    def test_a_second_loops_daemon_beside_the_supervisors_is_refused(self):
        # one clock by construction, not by instruction: the supervisor's
        # loops child holds run/loops.lock, so a stray `cousin-loops run`
        # on the same root (a leftover systemd unit, a hand-started one)
        # exits 5 (busy) instead of firing every beat, loop and one-shot twice
        self.assertTrue(_wait_for(self.both_landed),
                        "inbox: %r\n%s" % (self.inbox(), self.log()))
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root))
        stray = subprocess.run([sys.executable, "-m", "cousin_lib.loops", "run",
                                "--interval", "1", "--ticks", "1"],
                               env=env, cwd=REPO, capture_output=True, text=True,
                               timeout=30)
        self.assertEqual(stray.returncode, 5, stray.stderr)
        self.assertIn("another loops daemon holds", stray.stderr)
        time.sleep(2)                   # the supervisor's clock kept ticking alone
        rows = self.inbox()
        self.assertEqual(len([r for r in rows if r[2] == LOOP_PROMPT]), 1, rows)
        self.assertIsNone(self.proc.poll(), self.log())

    def test_sigterm_stops_the_clock_and_exits_0(self):
        self.assertTrue(_wait_for(self.both_landed),
                        "inbox: %r\n%s" % (self.inbox(), self.log()))
        children = _children(self.proc.pid)
        self.seen.update(children)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(45), 0, self.log())
        self.assertEqual(len(children), 2, children)
        for pid in children:            # reaped by the supervisor before it exited
            self.assertFalse(_alive(pid), "child %d outlived the supervisor" % pid)


@unittest.skipUnless(os.path.isdir("/proc/self"), "reads the process table from /proc")
class TestABusyClock(_ClockCase):
    """A stray `cousin-loops run` (an orphan of a
    SIGKILLed supervisor, a hand-started one, a unit still enabled)
    holds run/loops.lock BEFORE the supervisor starts. The supervisor's
    loops child exits 5, busy: it waits in `backoff`, never `failing`,
    and becomes the clock once the stray is gone. Counted as failures, it
    would be `failing` after five exits (about 15 s) and nothing would
    tick again."""

    BUSY = "another loops daemon holds its lock (exit 5)"
    COUNTED_EXITS = 5            # supervisor.MAX_EXITS: what used to mark it failing

    def loops_row(self):
        from cousin_lib import supervisor
        try:
            return supervisor.request(self.root, "status", timeout=5.0)["children"]["loops"]
        except (supervisor.SupervisorUnavailable, KeyError):
            return None

    def test_a_busy_clock_waits_for_the_holder_and_ticks_when_it_is_gone(self):
        from cousin_lib import supervisor
        self.assertEqual(supervisor.MAX_EXITS, self.COUNTED_EXITS)
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root), PYTHONUNBUFFERED="1")
        # the stray ticks once at its start (Wren's runner is not up, so
        # nothing is delivered), then sleeps an hour holding the lock
        stray = subprocess.Popen([sys.executable, "-m", "cousin_lib.loops", "run",
                                  "--interval", "3600"], env=env, cwd=REPO,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        self.addCleanup(stray.wait, 10)
        self.addCleanup(stray.kill)
        state = self.root / "data" / "loops-state.json"
        self.assertTrue(_wait_for(lambda: state.exists()), "the stray never ticked")
        self.assertIsNone(stray.poll())

        self.proc = self.supervise()
        busy = _wait_for(lambda: (self.loops_row() or {}).get("reason") == self.BUSY
                         and self.loops_row())
        self.assertTrue(busy, self.log())
        self.assertEqual(busy["state"], "backoff", busy)

        # past the exit that used to mark it failing, sampled over the socket
        states = set()

        def _busy_exits():
            row = self.loops_row()
            if row:
                states.add(row["state"])
            return self.log().count("supervisor: loops busy: %s" % self.BUSY) \
                >= self.COUNTED_EXITS
        self.assertTrue(_wait_for(_busy_exits, timeout=40), self.log())
        self.assertNotIn("failing", states, self.log())
        row = self.loops_row()
        self.assertEqual((row["state"], row["reason"]), ("backoff", self.BUSY), row)
        self.assertNotIn("supervisor: loops failing", self.log())
        self.assertEqual([r for r in self.inbox() if r[2] == LOOP_PROMPT], [],
                         "nothing ticks while the stray holds the lock")

        stray.kill()
        stray.wait(10)
        # the next retry (at most 60 s away; about 16 s here) takes the lock and ticks
        self.assertTrue(_wait_for(lambda: (self.loops_row() or {}).get("state") == "running",
                                  timeout=45), self.log())
        self.assertTrue(_wait_for(lambda: any(r[2] == LOOP_PROMPT for r in self.inbox()),
                                  timeout=DEADLINE_S), "inbox: %r\n%s" % (self.inbox(), self.log()))
        (loop,) = [r for r in self.inbox() if r[2] == LOOP_PROMPT]
        self.assertEqual((loop[0], loop[1]), ("loop:daemon", "loop"))
        self.assertIsNone(self.proc.poll(), self.log())


class TestTheRunnerHasNoClock(HermeticCase):
    """Guard: the runner package never ticks a schedule or a loop. It
    reads its inbox and waits on the wake socket; the schedule tool only
    ADDS a row to the store the loops daemon fires."""

    def test_the_runner_package_never_ticks(self):
        pattern = re.compile(r"cousin_lib\.loops|import loops|loops\.tick"
                             r"|schedule\.tick|threading\.Timer|sched\.scheduler")
        hits = []
        for path in sorted((REPO / "cousin_lib" / "runner").glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line.split("#", 1)[0]):
                    hits.append("%s:%d: %s" % (path.name, number, line.strip()))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
