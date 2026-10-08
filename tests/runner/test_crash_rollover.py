"""A runner killed mid-rollover (#286, point 4): a real process dies at a
crash point, a second one starts on the same home, and the stores must
show ONE rollover: the generation moved once, the first handoff stands,
the flip row closed delivered once, and no turn ran on the old session
after its handoff."""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # pragma: no cover - the sdk lane's tests need the SDK
    claude_agent_sdk = None

from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

ROOT = pathlib.Path(__file__).resolve().parents[2]

CHILD = r"""
import json, pathlib, sys, time
from cousin_lib.delivery import Item
from cousin_lib.runner import tools
from cousin_lib.runner.sdk import SdkRunner
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result

home, label = pathlib.Path(sys.argv[1]), sys.argv[2]
holder, clients, turns = {}, [], []
ARGS = {"position": "position " + label, "next_action": "next " + label,
        "status": "- loop " + label}

def note(sid, what):
    turns.append([sid, what])
    (home / ("turns-%s.json" % label)).write_text(json.dumps(turns))
    return None

def handoff(sid):
    return [init_msg(session=sid), ("CALL", lambda: note(sid, "handoff")),
            ("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", ARGS)),
            assistant(text="handed off"), result(session=sid)]

def plain(sid):
    return [init_msg(session=sid), ("CALL", lambda: note(sid, "turn")),
            assistant(text="ok"), result(session=sid)]

def factory(options):
    n = len(clients)
    sid = "s-1" if n == 0 else "s-%s-%d" % (label, n + 1)
    if n == 0 and label == "first":
        scripts = [plain(sid), handoff(sid)]
    elif n == 0:          # a resumed old session: whatever it is asked, it says so
        scripts = [handoff(sid)] + [plain(sid) for _ in range(4)]
    else:
        scripts = [plain(sid) for _ in range(6)]
    clients.append(ScriptedClient(options, scripts))
    return clients[-1]

r = SdkRunner(home, client_factory=factory, drain_timeout_s=2.0, handoff_deadline_s=10)
holder["r"] = r
# as runner/main does before start: a dead runner's claims are this one's
r.inbox.requeue_stale(older_than_s=0.0)
r.start()
deadline = time.time() + 30
if label == "first":
    rec = r.enqueue(Item("operator:priya", "chat", "work", sender="Priya"))
    while r.inbox.get(rec.inbox_id)["state"] != "done" and time.time() < deadline:
        time.sleep(0.05)
    r.rollover("max_age")
else:
    def busy():
        return any(row and row["state"] != "done"
                   for row in (r.inbox.get(i) for i in range(1, 40)))
    time.sleep(1.0)
    while (busy() or r.state() != "idle") and time.time() < deadline:
        time.sleep(0.1)
r.stop(timeout=5)
"""


@unittest.skipIf(claude_agent_sdk is None, "the sdk lane needs claude_agent_sdk")
class TestRolloverCrash(HermeticCase):
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

    def flip_rows(self):
        conn = sqlite3.connect(self.home / "data" / "inbox.db")
        try:
            return conn.execute("SELECT state, outcome FROM inbox WHERE source='flip'").fetchall()
        finally:
            conn.close()

    def turns(self, label):
        path = self.home / ("turns-%s.json" % label)
        return json.loads(path.read_text()) if path.exists() else []

    def assert_one_rollover(self):
        self.assertEqual((self.home / "data" / "generation.txt").read_text().strip(), "1")
        self.assertIn("position first", (self.home / "data" / "handoff.md").read_text())
        self.assertEqual(self.flip_rows(), [("done", "delivered")])
        # no turn of the restart ran on the old session
        self.assertEqual([t for t in self.turns("second") if t[0] == "s-1"], [])

    def test_killed_after_the_handoff_the_restart_finishes_that_rollover(self):
        first = self.child("first", crash="rollover.handed_off")
        self.assertEqual(first.returncode, -9, first.stderr[-2000:])
        second = self.child("second")
        self.assertEqual(second.returncode, 0, second.stderr[-3000:])
        self.assert_one_rollover()

    def test_killed_with_the_new_session_up_the_restart_does_not_roll_twice(self):
        first = self.child("first", crash="rollover.connected")
        self.assertEqual(first.returncode, -9, first.stderr[-2000:])
        second = self.child("second")
        self.assertEqual(second.returncode, 0, second.stderr[-3000:])
        self.assert_one_rollover()
