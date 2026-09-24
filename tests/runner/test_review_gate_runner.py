"""The review gate on the runner lane (master plan phase 7 task 10): after
every turn (a result or an error) and once at start, the entries written
on authored topics since the per-home cursor go through
review_gate.hold_new; over `[memory] review_batch` they are held and a
second model reviews them in one background task on the runner's loop,
never holding the next turn. The outcome is a `review_gate` event, never
a failed turn."""
import asyncio
import json
import os
import threading
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib import memory, review_gate, usage
from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _Base(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="sdk")
        (self.home.parent.parent / "config").mkdir(exist_ok=True)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)

    def _writes(self, n, topic="ledger fact %d"):
        def write():                      # the model's memory tool, n calls in one turn
            for i in range(n):
                memory.remember(self.home, topic % i, "The quokka ledger fact %d." % i)
        return write

    def _runner(self, script, **kw):
        factory = kw.pop("client_factory", None) or (lambda o: ScriptedClient(o, script))
        r = SdkRunner(self.home, client_factory=factory, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def _turn_script(self, n):
        return [init_msg(session="s-1"), ("CALL", self._writes(n)), assistant(text="ok"),
                result(session="s-1")]

    def _send(self, r, body="start"):
        return r.enqueue(Item("operator:priya", "chat", body, sender="Priya"))

    def _done(self, r, rec):
        return _wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done")

    def _gated(self, r):
        return [e["payload"] for e in r.events() if e["kind"] == "review_gate"]


class TestRunnerGate(_Base):
    def test_n_writes_in_a_turn_are_not_reviewed(self):
        seen = []
        r = self._runner([self._turn_script(3)], memory_reviewer=lambda rows: seen.append(rows) or {})
        r.start()
        self.assertTrue(self._done(r, self._send(r)))
        time.sleep(0.3)
        self.assertEqual((seen, self._gated(r)), ([], []))

    def test_n_plus_1_are_held_then_reviewed_by_the_second_model(self):
        def reviewer(rows):
            return {row["id"]: ("drop" if row["topic"] == "ledger fact 0" else "keep")
                    for row in rows}
        r = self._runner([self._turn_script(4)], memory_reviewer=reviewer)
        r.start()
        self.assertTrue(self._done(r, self._send(r)))
        self.assertTrue(_wait(lambda: self._gated(r)))
        [ev] = self._gated(r)
        self.assertEqual((ev["held"], ev["kept"], ev["dropped"], ev["pending"]), (4, 3, 1, 0))
        self.assertEqual(review_gate.pending(self.home), [])

    def test_a_failed_review_leaves_them_held_and_the_turn_delivered(self):
        def broken(rows):
            raise RuntimeError("reviewer down")
        r = self._runner([self._turn_script(4)], memory_reviewer=broken)
        r.start()
        rec = self._send(r)
        self.assertTrue(self._done(r, rec))
        self.assertTrue(_wait(lambda: self._gated(r)))
        [ev] = self._gated(r)
        self.assertIn("reviewer down", ev["error"])
        self.assertEqual(ev["pending"], 4)
        self.assertEqual(len(review_gate.pending(self.home)), 4)
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")


class TestBatches(_Base):
    def test_a_large_hold_is_reviewed_in_bounded_batches(self):
        """Review m6: one prompt never carries more than REVIEW_BATCH_MAX rows."""
        sizes = []
        r = self._runner([self._turn_script(45)],
                         memory_reviewer=lambda rows: sizes.append(len(rows)) or {})
        r.start()
        self.assertTrue(self._done(r, self._send(r)))
        self.assertTrue(_wait(lambda: len(self._gated(r)) == 3))
        self.assertEqual(sizes, [20, 20, 5])


class TestTheReviewNeverHoldsTheLoop(_Base):
    """Review I1: the review runs beside the loop, not inside the turn."""

    def test_the_next_row_runs_while_the_review_is_still_out(self):
        release = threading.Event()

        def slow(rows):
            release.wait(20)
            return {row["id"]: "keep" for row in rows}
        r = self._runner([self._turn_script(4), [assistant(text="second"), result(session="s-1")]],
                         memory_reviewer=slow)
        r.start()
        self.assertTrue(self._done(r, self._send(r)))
        second = self._send(r, "second")
        self.assertTrue(self._done(r, second), "the next row waited for the review")
        self.assertEqual(self._gated(r), [])                  # the review is still out
        release.set()
        self.assertTrue(_wait(lambda: self._gated(r)))
        self.assertEqual(self._gated(r)[0]["kept"], 4)

    def test_a_stop_cancels_the_review_and_leaves_the_entries_held(self):
        started = threading.Event()

        async def hanging(rows):
            started.set()
            await asyncio.sleep(3600)
        r = self._runner([self._turn_script(4)], memory_reviewer=hanging)
        r.start()
        self.assertTrue(self._done(r, self._send(r)))
        self.assertTrue(started.wait(10))
        t = time.monotonic()
        r.stop(timeout=10)
        self.assertLess(time.monotonic() - t, 8)
        self.assertEqual([e["error"] for e in self._gated(r)], ["cancelled"])
        self.assertEqual(len(review_gate.pending(self.home)), 4)


class TestTheGateFailsSafe(_Base):
    """Review I2: a turn that dies after its writes, and a runner that
    died before its gate, still have them held."""

    def test_a_turn_that_ends_in_an_error_after_its_writes_is_gated(self):
        r = self._runner([[init_msg(session="s-1"), ("CALL", self._writes(4)), "END"]],
                         memory_reviewer=lambda rows: {})
        r.start()
        rec = self._send(r)
        self.assertTrue(self._done(r, rec))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "failed")
        self.assertTrue(_wait(lambda: self._gated(r)))
        self.assertEqual(len(review_gate.pending(self.home)), 4)

    def test_a_start_holds_what_a_dead_runner_left_and_offers_it_at_most_twice(self):
        review_gate.begin(self.home, now=time.time() - 5)
        self._writes(4)()                                     # written, and the runner died
        offered = []
        for _ in range(3):
            r = self._runner([], memory_reviewer=lambda rows: offered.append(len(rows)) or {})
            r.start()
            self.assertTrue(_wait(lambda: self._gated(r)) or len(offered) >= 2)
            r.stop(timeout=5)
        self.assertEqual(offered, [4, 4])                    # MAX_ATTEMPTS, then the operator's
        self.assertEqual(len(review_gate.pending(self.home)), 4)


class TestOneReviewerPerHome(_Base):
    """Phase 7b review round 2, N2: a side session (phase 8) runs `_main`
    too; only the primary sweeps the held entries at start, or each
    session would review the same rows and use up their attempts."""

    def test_a_primary_and_a_side_session_review_a_held_batch_once(self):
        from cousin_lib.runner import sessions
        review_gate.begin(self.home, now=time.time() - 5)
        self._writes(4)()
        review_gate.hold_new(self.home)                       # held by a runner that died
        calls = []

        def reviewer(rows):
            calls.append(len(rows))
            return {}
        primary = self._runner([], memory_reviewer=reviewer)
        side = sessions.SideSession(self.home, kind="peer", memory_reviewer=reviewer,
                                    client_factory=lambda o: ScriptedClient(o, []),
                                    primary_activity=lambda: None)
        self.addCleanup(lambda: side.stop(timeout=5))
        side.start()
        primary.start()
        self.assertTrue(_wait(lambda: calls))
        time.sleep(1.0)
        self.assertEqual(calls, [4])
        self.assertFalse(sessions.SideSession.sweeps_at_start)


class TestTheDefaultReviewer(_Base):
    def test_one_tool_less_call_on_the_configured_model_with_its_usage_recorded(self):
        """No `memory_reviewer`: a fresh client, no tools, no MCP servers,
        one turn, a fixed cwd under data/, `[memory] review_model` (review
        M6), and its usage in the usage store."""
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[memory]\nreview_model = "claude-haiku-4-5-20251001"\n')
        made = []

        def factory(options):
            if not made:                  # the cousin's own session
                made.append(ScriptedClient(options, [self._turn_script(4)]))
            else:
                verdict = {row["id"]: "keep" for row in review_gate.pending(self.home)}
                made.append(ScriptedClient(options, [[
                    assistant(text="Verdict:\n" + json.dumps(verdict)),
                    result(session="review", usage={"input_tokens": 50, "output_tokens": 7})]]))
            return made[-1]
        r = self._runner(None, client_factory=factory)
        r.start()
        self.assertTrue(self._done(r, self._send(r)))
        self.assertTrue(_wait(lambda: self._gated(r)))
        [ev] = self._gated(r)
        self.assertEqual((ev["held"], ev["kept"], ev["error"]), (4, 4, None))
        opts = made[1].options
        self.assertEqual((opts.tools, opts.mcp_servers, opts.max_turns), ([], {}, 1))
        self.assertIn("no-session-persistence", opts.extra_args)
        self.assertEqual(opts.model, "claude-haiku-4-5-20251001")
        self.assertEqual(str(opts.cwd), str(self.home / "data" / "review-cwd"))
        self.assertIn("ledger fact 3", str(made[1].queries))
        with usage._db(self.home) as conn:
            sessions = [row[0] for row in conn.execute("SELECT session_id FROM usage")]
        self.assertIn("review", sessions)


class TestParse(unittest.TestCase):
    def test_a_verdict_is_read_from_the_first_json_object_and_unknown_ids_ignored(self):
        text = 'Here: {"aaaaaaaaaaaa": "keep", "bbbbbbbbbbbb": "DROP", "zzz": "keep"} done'
        self.assertEqual(review_gate.parse_verdicts(text, ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]),
                         {"aaaaaaaaaaaa": "keep", "bbbbbbbbbbbb": "drop"})
        self.assertEqual(review_gate.parse_verdicts("no json here", ["aaaaaaaaaaaa"]), {})


if __name__ == "__main__":
    unittest.main()
