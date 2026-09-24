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
