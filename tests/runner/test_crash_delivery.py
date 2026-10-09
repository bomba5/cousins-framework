"""A runner killed while delivering a row (#286, point 2): a real process
dies at a crash point, a second one starts on the same home as
runner/main does (close_recorded, then requeue_stale), and the stores
must show the row closed once and its turn run once.

The tmux lane's pane outlives its runner, so there the runner's thread
is what dies (crashpoint.arm), and a second runner adopts the same pane."""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # pragma: no cover - the sdk lane's tests need the SDK
    claude_agent_sdk = None

from cousin_lib import crashpoint
from cousin_lib.delivery import Item
from cousin_lib.runner.tmux_runner import TmuxRunner
from tests._hermetic import HermeticCase
from tests.runner._fake_pane import FakePane
from tests.runner._home import temp_home

ROOT = pathlib.Path(__file__).resolve().parents[2]

CHILD = r"""
import json, pathlib, sys, time
from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result

home, label = pathlib.Path(sys.argv[1]), sys.argv[2]
turns = []

def note(sid, what):
    turns.append([sid, what])
    (home / ("turns-%s.json" % label)).write_text(json.dumps(turns))
    return None

def plain(sid):
    return [init_msg(session=sid), ("CALL", lambda: note(sid, "turn")),
            assistant(text="ok"), result(session=sid)]

def factory(options):
    return ScriptedClient(options, [plain("s-1") for _ in range(4)])

r = SdkRunner(home, client_factory=factory, drain_timeout_s=2.0)
# as runner/main does before start: what a dead runner recorded is closed,
# the rest of its claims are this one's
r.inbox.close_recorded()
r.inbox.requeue_stale(older_than_s=0.0)
r.start()
deadline = time.time() + 30
if label == "first":
    rec = r.enqueue(Item("operator:priya", "chat", "work", sender="Priya"))
    while r.inbox.get(rec.inbox_id)["state"] != "done" and time.time() < deadline:
        time.sleep(0.05)
else:
    time.sleep(1.0)
    while (r.inbox.pending() or r.state() != "idle") and time.time() < deadline:
        time.sleep(0.1)
r.stop(timeout=5)
"""


@unittest.skipIf(claude_agent_sdk is None, "the sdk lane needs claude_agent_sdk")
class TestSdkDeliveryCrash(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        self.env = dict(os.environ, PYTHONPATH=str(ROOT), FRAMEWORK_ROOT=str(root))
        self.env.pop("COUSIN_CRASH_AT", None)

    def child(self, label, crash=None):
        env = dict(self.env, **({"COUSIN_CRASH_AT": crash} if crash else {}))
        return subprocess.run([sys.executable, "-c", CHILD, str(self.home), label],
                              env=env, capture_output=True, text=True, timeout=120)

    def rows(self):
        conn = sqlite3.connect(self.home / "data" / "inbox.db")
        try:
            return conn.execute("SELECT state, outcome, detail FROM inbox"
                                " WHERE source='chat'").fetchall()
        finally:
            conn.close()

    def turns(self, label):
        path = self.home / ("turns-%s.json" % label)
        return json.loads(path.read_text()) if path.exists() else []

    def test_killed_after_the_result_was_recorded_the_turn_does_not_run_again(self):
        first = self.child("first", crash="runner.result_recorded")
        self.assertEqual(first.returncode, -9, first.stderr[-2000:])
        self.assertEqual(len(self.turns("first")), 1)
        second = self.child("second")
        self.assertEqual(second.returncode, 0, second.stderr[-3000:])
        (row,) = self.rows()
        self.assertEqual(row[:2], ("done", "delivered"))
        self.assertIn("closed at restart", row[2])
        self.assertEqual(self.turns("second"), [], "the turn ran again")

    def test_killed_after_the_write_the_row_runs_once_on_the_restart(self):
        first = self.child("first", crash="sdk.written")
        self.assertEqual(first.returncode, -9, first.stderr[-2000:])
        self.assertEqual(self.turns("first"), [])
        second = self.child("second")
        self.assertEqual(second.returncode, 0, second.stderr[-3000:])
        (row,) = self.rows()
        self.assertEqual(row[:2], ("done", "delivered"))
        self.assertEqual(len(self.turns("second")), 1)


WAIT_S = 30.0


def _wait(pred, timeout=WAIT_S):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestTmuxDeliveryCrash(HermeticCase):
    def runner(self, factory):
        r = TmuxRunner(self.home, account=None, pane_factory=factory,
                       config_dir=self.home / ".cfg",
                       launch_argv=lambda sid, fresh: ["claude", sid] + (["--fresh"] if fresh else []))
        self.addCleanup(FakePane.quiesce_all)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def test_killed_after_a_line_was_handled_the_restart_closes_the_row_once(self):
        """The turn end is handled (the row closed), the cursor not yet saved:
        the adopting runner reads that line again and must not close,
        retype or run anything twice."""
        self.home = temp_home(self, runner="tmux")
        shared, holder = [], {}

        def one_pane(path):
            if not shared:
                shared.append(FakePane(path, context_home=self.home))
            return shared[0]

        def kill_after_the_close():
            # COUSIN_CRASH_AT=tmux.handled at the line that closed the row
            rec = holder.get("rec")
            if rec is None or holder["r"].inbox.get(rec.inbox_id)["state"] != "done":
                return False
            raise SystemExit("killed at a crash point")

        r = holder["r"] = self.runner(one_pane)
        r.start()
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.addCleanup(crashpoint.arm("tmux.handled", kill_after_the_close))
        holder["rec"] = rec = r.enqueue(Item("operator:wren", "chat", "one", sender="Wren"))
        self.assertTrue(_wait(lambda: not r._thread.is_alive()), "the runner never died")
        saved = json.loads((self.home / "data" / "tmux-cursor.json").read_text())["offset"]
        self.assertLess(saved, r._path.stat().st_size, "the cursor was saved past the line")
        r2 = self.runner(one_pane)
        r2.start()
        self.assertTrue(_wait(lambda: r2.state() == "idle"))
        time.sleep(0.5)
        row = r2.inbox.get(rec.inbox_id)
        self.assertEqual((row["state"], row["outcome"]), ("done", "delivered"))
        self.assertEqual(len(shared[0].typed), 1, "nothing typed again")
        self.assertEqual([e for e in r2.events() if e["kind"] == "result"], [],
                         "a second result for the same turn")

if __name__ == "__main__":
    unittest.main()
