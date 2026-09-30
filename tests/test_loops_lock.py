"""One clock per root: `cousin-loops run` holds <root>/run/loops.lock for
its life, and a second daemon on the same root exits 5, busy (phase 6
task 1, review C2; round 2 N1: busy, not configuration, so a supervisor
waits for the holder instead of marking its loops child `failing`). Two daemons would each fire every due one-shot, heartbeat,
[[loops]] entry and daily flip. Every process is killed in cleanup and
every wait has a deadline."""
import fcntl
import os
import pathlib
import subprocess
import sys
import tempfile
import time

from cousin_lib import loops
from cousin_lib.loops import hold_loops_lock
from tests._hermetic import HermeticCase


def _held(path):
    """True when another process holds an exclusive flock on `path`."""
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class TestLoopsLock(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "root"
        self.root.mkdir()
        self.lock = self.root / "run" / "loops.lock"
        self.env = dict(os.environ, FRAMEWORK_ROOT=str(self.root), PYTHONUNBUFFERED="1")

    def _run(self, *args, timeout=30):
        return subprocess.run([sys.executable, "-m", "cousin_lib.loops", "run"] + list(args),
                              env=self.env, capture_output=True, text=True, timeout=timeout)

    def test_a_second_daemon_on_the_same_root_exits_5_and_the_first_keeps_running(self):
        first = subprocess.Popen([sys.executable, "-m", "cousin_lib.loops", "run",
                                  "--interval", "30"], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(first.wait, 10)
        self.addCleanup(first.kill)
        deadline = time.monotonic() + 20
        while not _held(self.lock) and time.monotonic() < deadline:
            self.assertIsNone(first.poll(), "the first daemon exited before taking its lock")
            time.sleep(0.05)
        self.assertTrue(_held(self.lock), "the first daemon never took %s" % self.lock)

        second = self._run("--ticks", "1")
        self.assertEqual(loops.LOCK_HELD_EXIT, 5)
        self.assertEqual(second.returncode, loops.LOCK_HELD_EXIT, second.stderr)
        self.assertIn("another loops daemon holds %s" % self.lock, second.stderr)
        self.assertIsNone(first.poll(), "the first daemon must keep running")
        self.assertTrue(_held(self.lock))

        first.kill()
        first.wait(10)
        third = self._run("--ticks", "1")               # released with its process: ours now
        self.assertEqual(third.returncode, 0, third.stderr)

    def test_the_lock_is_released_when_run_returns(self):
        # in-process `run --ticks N` (tests, one-shot callers) gives the lock back
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        self.assertEqual(loops.loops_main(["run", "--ticks", "1", "--interval", "0"]), 0)
        self.assertFalse(_held(self.lock))
        self.assertEqual(loops.loops_main(["run", "--ticks", "1", "--interval", "0"]), 0)
        self.assertEqual(oct(os.stat(self.lock.parent).st_mode & 0o777), "0o700")


class TestTheForkHandlerClosesOnlyItsOwnLock(HermeticCase):
    """A handler outlives its lock (one per call, never unregistered).
    Once the lock file is gone its inode can be recycled for a new file
    that a reused fd number then names: device and inode match, and the
    stale handler closed a descriptor that was not its own (#92)."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        (self.dir / "root").mkdir()

    def _handler(self):
        from unittest import mock
        handlers = []
        with mock.patch.object(os, "register_at_fork",
                               lambda **kw: handlers.append(kw["after_in_child"])):
            fd = hold_loops_lock(self.dir / "root")
        return fd, handlers[0]

    def test_a_recycled_inode_under_another_path_is_left_open(self):
        from unittest import mock
        fd, handler = self._handler()
        stale = os.fstat(fd)
        other = os.open(self.dir / "unrelated", os.O_RDWR | os.O_CREAT, 0o600)
        os.dup2(other, fd)                        # the lock closed, its fd number reused
        os.close(other)
        self.addCleanup(lambda: os.close(fd))
        real_fstat = os.fstat
        # the recycled inode: fstat of the reused fd reads the old identity
        with mock.patch.object(os, "fstat",
                               lambda n: stale if n == fd else real_fstat(n)):
            handler()
        os.fstat(fd)                              # still open: not closed by the handler

    def test_its_own_lock_is_closed(self):
        fd, handler = self._handler()
        handler()
        with self.assertRaises(OSError):
            os.fstat(fd)


class TestLoopsLockSurvivesFork(HermeticCase):
    """jobs._spawn_tracked forks (twice, no exec) from inside the loops
    daemon's own process to run a worker loop's command; flock locks are
    shared across fork, so the job-runner child inherits the daemon's
    copy of run/loops.lock and, without a fix, holds it open until the
    job ends - the daemon can die and the clock stays held (phase 6 fix
    wave item 1). hold_loops_lock must close its own fd in every forked
    child from then on, tolerant of a fd already closed."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "root"
        self.root.mkdir()

    def test_a_forked_child_does_not_keep_the_lock_held(self):
        fd = hold_loops_lock(self.root)
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            os.write(write_fd, b"up\n")
            os.close(write_fd)
            time.sleep(10)
            os._exit(0)
        os.close(write_fd)
        try:
            with os.fdopen(read_fd) as fh:
                self.assertEqual(fh.readline(), "up\n",
                                 "the forked child never signalled it was up")
            os.close(fd)     # the parent gives up its own copy, as the daemon does on exit
            self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0),
                             "the forked child must still be alive for this to test anything")
            second = hold_loops_lock(self.root)
            os.close(second)
        finally:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            os.waitpid(pid, 0)
