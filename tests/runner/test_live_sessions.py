"""Side sessions against the real SDK (phase 8). Opt in: COUSIN_LIVE_SDK=1.
Costs a few small model calls on whatever lane the machine has.

The exit criterion by effect: a person is answered, through the reply
tool, while the primary session is still inside a long task; and the side
session does not re-create the law block the primary created (phase 4 R1:
same prompt, same tools, same working directory). The bar is phase 4's
(tests/runner/test_live_prompt.py, ruling W9-5): a per-run nonce first in
the law makes the primary create the block cold, and the side session's
cumulative cache creation must stay under the primary's minus a lower bound
on the law's tokens. `cache_read > 0` alone proves nothing: the turn's second
model call reads what its first call wrote (review I2)."""
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
import uuid

from cousin_lib.delivery import Item
from cousin_lib.runner import sessions
from tests._hermetic import HermeticCase
from tests.runner.test_live_prompt import LAW as _PHASE4_LAW

# This module's own nonce (round 2 review N4): phase 4's law, re-headed, so a
# run after tests/runner/test_live_prompt.py in one process starts cold too.
LAW = "Run %s.\n" % uuid.uuid4().hex + _PHASE4_LAW.split("\n", 1)[1]
LAW_TOKENS_LOWER_BOUND = len(LAW) // 6

MODEL = "claude-haiku-4-5-20251001"


def _wait(pred, timeout, step=0.05):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveSideSession(HermeticCase):
    def test_a_person_is_answered_while_the_primary_runs_a_long_task(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "config").mkdir()
        (root / "config" / "law.md").write_text(LAW)
        home = root / "cousins" / "wren"
        for sub in ("data", "run", "memory"):
            (home / sub).mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Priya"\n\n'
            '[agent]\nrunner = "sdk"\n\n[agent.sessions]\nperson = "own"\n')
        os.environ["FRAMEWORK_ROOT"] = str(root)
        s = sessions.Sessions(home, kinds=("person",), model=MODEL, idle_timeout_s=180)
        self.addCleanup(lambda: s.stop(timeout=30))
        s.start()
        op = s.enqueue(Item("operator:priya", "chat",
                            "Run the Bash command `sleep 40`, then reply only DONE.",
                            sender="Priya"))
        # the primary's first model call answered (it asked for Bash): the
        # prefix it created cold is in the cache before the side session asks
        self.assertTrue(_wait(lambda: any(e["kind"] == "tool" for e in s.primary.events()), 90))
        person = s.enqueue(Item("person:mallory", "chat",
                                "Answer me with the reply tool, the text exactly: PONG",
                                sender="Mallory"))
        self.assertTrue(_wait(lambda: s.inbox.get(person.inbox_id)["state"] == "done", 120),
                        "the side session never answered")
        self.assertEqual(s.inbox.get(op.inbox_id)["state"], "claimed",
                         "the primary's long task finished first: nothing was proven")
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            rows = conn.execute("SELECT chat_user, message FROM messages").fetchall()
        finally:
            conn.close()
        self.assertTrue(any(u == "mallory" and "PONG" in m for u, m in rows), rows)
        self.assertTrue(_wait(lambda: s.inbox.get(op.inbox_id)["state"] == "done", 180))
        side = [e["payload"] for e in s.sides["person"].events() if e["kind"] == "result"][0]
        primary = [e["payload"] for e in s.primary.events() if e["kind"] == "result"][0]
        created_side = (side["usage"] or {}).get("cache_creation_input_tokens") or 0
        created_primary = (primary["usage"] or {}).get("cache_creation_input_tokens") or 0
        read_side = (side["usage"] or {}).get("cache_read_input_tokens") or 0
        print("\nREPORT side cache: primary created=%d; side created=%d read=%d (bound %d)"
              % (created_primary, created_side, read_side, LAW_TOKENS_LOWER_BOUND))
        self.assertLess(created_side, created_primary - LAW_TOKENS_LOWER_BOUND,
                        "the side session re-created the law block: %r vs %r" % (side, primary))
        self.assertGreater(read_side, 0)


if __name__ == "__main__":
    unittest.main()
