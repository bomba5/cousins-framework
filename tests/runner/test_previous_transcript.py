"""A cousin moved from the tmux lane gets its previous transcript's
path in the first message of its first fresh session, once."""
import json
import os
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_rollover_runner import RolloverCase
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result

LAST = "/scratch/transcripts/-x-cousins-wren/5e55a0de-last.jsonl"
BEFORE = "/scratch/transcripts/-x-cousins-wren/0ld5e55a-before.jsonl"
ENDED = "2026-09-24T10:11:12+00:00"
RECORD = {"lane": "tmux", "ended_at": ENDED, "session_id": "5e55a0de-last", "missing": None,
          "transcripts": [{"which": "last", "session_id": "5e55a0de-last", "path": LAST},
                          {"which": "before", "session_id": "0ld5e55a-before", "path": BEFORE}]}


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _write_record(home, rec=RECORD):
    (home / "data" / "previous-transcript.json").write_text(json.dumps(rec))


class TestFirstFreshStart(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)
        (self.home / "STATUS.md").write_text("## Open loops\n- carry this\n")

    def runner(self):
        def factory(options):
            return ScriptedClient(options, [[init_msg(session="s-live"), assistant(text="ok"),
                                             result(session="s-live")] for _ in range(4)])
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def digests(self, r):
        return [b for b in ((r.inbox.get(i) or {}).get("body") or "" for i in range(1, 30))
                if "STATE DIGEST" in b]

    def test_the_first_fresh_start_hands_the_path_once(self):
        _write_record(self.home)
        r = self.runner(); r.start()
        self.assertTrue(_wait(lambda: self.digests(r)))
        first = self.digests(r)[0]
        self.assertIn(LAST, first)
        self.assertIn(BEFORE, first)
        self.assertIn(ENDED, first)
        self.assertIn("read-only", first)
        self.assertIn("subagent", first)
        self.assertIn("from the end", first)
        self.assertIn("STATUS", first)
        # the paragraph comes after the digest, never before it
        self.assertLess(first.index("STATE DIGEST"), first.index(LAST))
        self.assertTrue(_wait(lambda: not (self.home / "data" / "previous-transcript.json")
                              .exists()))
        self.assertTrue((self.home / "data" / "previous-transcript.json.consumed").exists())

    def test_a_second_fresh_start_does_not_repeat_it(self):
        _write_record(self.home)
        r1 = self.runner(); r1.start()
        self.assertTrue(_wait(lambda: self.digests(r1)))
        rec = r1.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r1.inbox.get(rec.inbox_id)["state"] == "done"))
        r1.stop(timeout=5)
        (self.home / "data" / "runner-session.json").unlink()      # the next start is fresh
        r2 = self.runner(); r2.start()
        self.assertTrue(_wait(lambda: len(self.digests(r2)) == 2))
        self.assertIn(LAST, self.digests(r2)[0])
        self.assertNotIn(LAST, self.digests(r2)[1])
        self.assertNotIn("before the move", self.digests(r2)[1])

    def test_a_missing_transcript_is_said_plainly(self):
        _write_record(self.home, dict(RECORD, transcripts=[], session_id=None,
                                      missing="no runtime.session_id in cousin.toml"))
        r = self.runner(); r.start()
        self.assertTrue(_wait(lambda: self.digests(r)))
        self.assertIn("no runtime.session_id in cousin.toml", self.digests(r)[0])

    def test_the_paragraph_is_byte_stable(self):
        from cousin_lib import handover
        _write_record(self.home)
        one = handover.note(self.home)
        time.sleep(1.1)
        self.assertEqual(one, handover.note(self.home))
        self.assertNotIn(time.strftime("%Y-%m-%dT%H:%M"), one.replace(ENDED, ""))

    def test_no_record_no_paragraph(self):
        r = self.runner(); r.start()
        self.assertTrue(_wait(lambda: self.digests(r)))
        self.assertNotIn("before the move", self.digests(r)[0])


class TestRolloverFindsIt(RolloverCase):
    def test_a_rollover_that_finds_the_record_hands_it(self):
        r = self.build(); r.start(); self.work(r)
        _write_record(self.home)
        out = r.rollover("context pressure")
        self.assertTrue(out["ok"], out)
        self.assertTrue(_wait(lambda: self.clients[1].queries))
        first = self.clients[1].queries[0]["message"]["content"][0]["text"]
        self.assertIn("STATE DIGEST FOR COUSIN: wren", first)
        self.assertIn(LAST, first)
        self.assertFalse((self.home / "data" / "previous-transcript.json").exists())


if __name__ == "__main__":
    unittest.main()
