"""SdkRunner.rollover end to end on the scripted client."""
import asyncio
import json
import os
import threading
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib import boot
from cousin_lib.delivery import Item
from cousin_lib.runner import prompt, tools
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, asked_resume, assistant, init_msg, result

ARGS = {"position": "Mid-audit.", "next_action": "Reconcile March.",
        "status": "- audit: March open", "active_threads": ["audit - March"]}


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class RolloverCase(HermeticCase):
    def build(self, *, handoff=True, hooks=False, deadline=5.0, slow_first=False, handoff_delay=0.0):
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        # the runner's own root, never the environment's (review C2)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)
        if hooks:
            with open(self.home / "cousin.toml", "a") as fh:
                fh.write('\n[session]\nstart_hooks = ["echo start >> %s/hooks.log"]\n'
                         'end_hooks = ["touch %s/ended"]\n' % (self.home, self.home))
        self.clients, self.ended_before_new_client = [], None
        holder = {}

        def handoff_turn():
            steps = [init_msg(session="s-1")]
            if handoff_delay:
                steps.append(("SLOW", handoff_delay))
            if not handoff:
                return steps + ["WAIT_FOR_INTERRUPT"]
            return steps + [("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", ARGS)),
                            assistant(text="handed off"), result(session="s-1")]

        def factory(options):
            n = len(self.clients)
            sid = "s-%d" % (n + 1)
            if n == 0:   # generation 1: one work turn, then the handoff turn
                work = [init_msg(session=sid)] + ([("SLOW", 1.0)] if slow_first else []) + \
                       [assistant(text="work"), result(session=sid)]
                scripts = [work, handoff_turn()]
            else:        # generation 2: the digest turn, then anything
                self.ended_before_new_client = (self.home / "ended").exists()
                scripts = [[init_msg(session=sid), assistant(text="ok"), result(session=sid)]
                           for _ in range(8)]
            self.clients.append(ScriptedClient(options, scripts))
            return self.clients[-1]
        r = SdkRunner(self.home, client_factory=factory, drain_timeout_s=2.0,
                      handoff_deadline_s=deadline)
        holder["r"] = r
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def op(self, r, body):
        return r.enqueue(Item("operator:priya", "chat", body, sender="Priya"))

    def work(self, r, body="start the audit"):
        rec = self.op(r, body)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
        return rec


class TestRollover(RolloverCase):
    def test_handoff_awaited_new_session_digest_first(self):
        r = self.build(); r.start(); self.work(r)
        out = r.rollover("context pressure")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["handoff"], "clean")
        self.assertEqual(out["generation"], 1)
        self.assertEqual(len(self.clients), 2)                          # a new client
        self.assertIsNone(asked_resume(self.clients[1].options))      # a fresh session
        self.assertTrue(_wait(lambda: self.clients[1].queries))
        first = self.clients[1].queries[0]["message"]["content"][0]["text"]
        self.assertIn("STATE DIGEST FOR COUSIN: wren", first)          # digest as first message
        self.assertIn("March open", first)                             # it carries the handoff
        self.assertIn("Reconcile March", (self.home / "data" / "handoff.md").read_text())
        marks = {e["payload"].get("phase"): e["seq"] for e in r.events() if e["kind"] == "rollover"}
        self.assertTrue(any(e["kind"] == "usage" and marks["start"] < e["seq"] < marks["done"]
                            for e in r.events()), "the handoff turn's cost was not recorded")

    def test_the_prompt_passed_to_both_clients_is_the_same_bytes(self):
        r = self.build(); r.start(); self.work(r)
        r.rollover("max_age")
        a, b = (c.options.system_prompt["append"] for c in self.clients)
        self.assertEqual(a.encode(), b.encode())

    def test_an_unanswered_handoff_ends_in_an_emergency_handoff_from_the_real_transcript(self):
        r = self.build(handoff=False, deadline=0.5); r.start(); self.work(r)
        entry = {"type": "assistant", "uuid": "tail-1", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "the ledger is half reconciled"}]}}
        asyncio.run(r.session_store.append({"project_key": "p", "session_id": "s-1"}, [entry]))
        out = r.rollover("max_age")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["handoff"], "emergency")
        handoff = (self.home / "data" / "handoff.md").read_text()
        self.assertIn("degraded_state: true", handoff)
        self.assertIn("handoff timeout", handoff)                      # the deadline branch
        self.assertIn("the ledger is half reconciled", handoff)        # from the store's tail
        self.assertGreaterEqual(self.clients[0].interrupts, 1)         # the turn was interrupted
        self.assertEqual(len(self.clients), 2)

    def test_rows_queued_across_a_rollover_each_run_exactly_once(self):
        r = self.build(handoff_delay=0.5); r.start()
        before = self.work(r, "before")
        t = threading.Thread(target=r.rollover, args=("max_age",)); t.start()
        self.assertTrue(_wait(lambda: r.state() == "rolling_over"))    # no race: it is mid-handoff
        during = self.op(r, "during")
        t.join(15)
        after = self.op(r, "after")
        for rec in (before, during, after):
            self.assertTrue(_wait(lambda rec=rec: r.inbox.get(rec.inbox_id)["state"] == "done"))
            self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        texts = [q["message"]["content"][0]["text"] for c in self.clients for q in c.queries]
        for body in ("before", "during", "after"):
            self.assertEqual(sum(("\n\n%s" % body) in t for t in texts), 1, body)
        self.assertTrue(self.clients[1].queries[0]["message"]["content"][0]["text"]
                        .count("STATE DIGEST"), "the digest ran before 'during'")

    def test_a_rollover_never_interrupts_a_live_turn(self):
        r = self.build(slow_first=True); r.start()
        rec = self.op(r, "long work")
        self.assertTrue(_wait(lambda: r.state() == "running"))       # the turn is live for 1 s
        out = r.rollover("max_age")                                    # asked mid-turn
        self.assertTrue(out["ok"])
        self.assertEqual(self.clients[0].interrupts, 0)
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")

    def test_end_hooks_run_before_the_new_client_and_start_hooks_once(self):
        r = self.build(hooks=True); r.start(); self.work(r)
        r.rollover("max_age")
        self.assertIs(self.ended_before_new_client, True)
        self.assertEqual((self.home / "hooks.log").read_text().split(), ["start", "start"])

    def test_the_generation_counter_moves_once(self):
        r = self.build(); r.start(); self.work(r)
        g0 = boot.read_generation(self.home)
        r.rollover("max_age")
        self.assertEqual(boot.read_generation(self.home), g0 + 1)
        self.assertTrue((self.home / "data" / "generations" / ("gen-%04d" % g0)).is_dir())

    def test_stop_during_the_handoff_requeues_the_flip_row(self):
        r = self.build(handoff=False, deadline=30.0); r.start()
        self.work(r)                                   # so the handoff turn is the hanging one
        t = threading.Thread(target=r.rollover, args=("max_age",)); t.start()
        self.assertTrue(_wait(lambda: r.state() == "rolling_over"
                              and len(self.clients[0].queries) == 2))
        r.stop(timeout=5)
        t.join(10)
        flips = r.inbox.open_rows("flip")
        self.assertEqual([row["state"] for row in flips], ["queued"])   # the next start finishes it

    def test_a_failure_before_the_new_session_is_errored_then_idle(self):
        from cousin_lib.runner import rollover as rollover_mod
        r = self.build(); r.start(); self.work(r)
        with mock.patch.object(rollover_mod, "archive_generation", side_effect=OSError("disk gone")):
            out = r.rollover("max_age")
        self.assertFalse(out["ok"])
        self.assertIn("disk gone", out["reason"])
        self.assertEqual(out["generation"], 0)                         # never bumped
        self.assertIsNone(out["new_session"])
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        rec = self.work(r, "still alive")                              # the next row runs
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertIn("errored", states)

    def test_a_failing_digest_falls_back_to_a_degraded_one(self):
        r = self.build(); r.start(); self.work(r)
        with mock.patch.object(prompt, "state_digest", side_effect=RuntimeError("layer broke")):
            out = r.rollover("max_age")
        self.assertTrue(out["ok"], out)
        self.assertIn("degraded", out["digest"])
        self.assertEqual(out["generation"], 1)
        self.assertTrue(_wait(lambda: self.clients[1].queries))
        first = self.clients[1].queries[0]["message"]["content"][0]["text"]
        self.assertIn("DEGRADED", first)
        self.assertIn("Reconcile March", first)                        # the handoff, verbatim

    def test_a_bequest_racing_a_pressure_rollover_is_not_coalesced_away(self):
        bequest = ("You are about to be reincarnated: your role changes, your memory persists.\n"
                   "Put a bequest to your successor in the handoff's position field.")
        r = self.build(handoff_delay=0.5); r.start(); self.work(r)
        r._request_rollover("context pressure 91%")
        self.assertTrue(_wait(lambda: r.state() == "rolling_over"))   # the plain one is running
        out = r.rollover(bequest)                                      # its own row, its own rollover
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["coalesced"])
        texts = [q["message"]["content"][0]["text"] for c in self.clients for q in c.queries]
        self.assertTrue(any("(context pressure 91%)" in t for t in texts))   # the plain request asked
        self.assertTrue(any("You are about to be reincarnated" in t for t in texts))  # and the bequest
        self.assertEqual(len(self.clients), 3)                          # two rollovers, not one
        self.assertEqual(boot.read_generation(self.home), 2)

    def test_two_requests_make_one_rollover(self):
        r = self.build(handoff_delay=0.5); r.start(); self.work(r)
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(r.rollover("max_age")))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(len(self.clients), 2)                         # one new client, not two
        self.assertTrue(all(a["ok"] for a in answers))
        self.assertEqual(boot.read_generation(self.home), 1)


class TestRolloverFailureModes(RolloverCase):
    """Fix round 1: what a rollover does when a step fails on either side
    of the point of no return (the new session existing)."""

    def failing_connects(self, r, fail_calls):
        """Every client the runner asks for, in order, is recorded in
        self.asked (its options); the calls numbered in fail_calls raise."""
        real, self.asked = r.client_factory, []

        def factory(options):
            self.asked.append(options)
            if len(self.asked) in fail_calls:
                raise ConnectionError("no CLI (call %d)" % len(self.asked))
            return real(options)
        r.client_factory = factory

    def test_a_failed_new_session_falls_back_to_the_old_one(self):
        r = self.build(); self.failing_connects(r, {2}); r.start(); self.work(r)
        out = r.rollover("max_age")
        self.assertFalse(out["ok"]); self.assertIn("could not start the new session", out["reason"])
        self.assertEqual((out["generation"], out["new_session"]), (0, None))
        self.assertEqual([asked_resume(o) for o in self.asked], [None, None, "s-1"])
        self.assertEqual(r._resume_id, "s-1")     # the old session, not None, until the next init
        self.assertEqual(r.saved_session(), "s-1")   # on file too: a restart resumes it (Task 11)
        self.assertTrue(any(e["kind"] == "system" and e["payload"].get("subtype") == "connect_failed"
                            for e in r.events()))
        rec = self.work(r, "still alive")
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        self.assertEqual(boot.read_generation(self.home), 0)

    def test_when_the_fallback_fails_too_the_next_reconnect_resumes_the_old_session(self):
        r = self.build(); self.failing_connects(r, {2, 3}); r.start(); self.work(r)
        out = r.rollover("max_age")
        self.assertFalse(out["ok"])
        rec = self.work(r, "still alive")                 # no client: _NotWritten, then _resync
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        self.assertEqual(len(self.asked), 4)
        self.assertEqual(asked_resume(self.asked[3]), "s-1")   # never a fresh, unbumped, digest-less one
        self.assertEqual(boot.read_generation(self.home), 0)
        texts = [q["message"]["content"][0]["text"] for c in self.clients for q in c.queries]
        self.assertFalse(any("STATE DIGEST" in t for t in texts))

    def test_a_failing_start_hook_after_the_new_session_degrades_never_fails(self):
        from cousin_lib import session as session_mod
        real = session_mod.run_phase

        def run_phase(home, phase, *a, **kw):
            if phase == "start":
                raise RuntimeError("start hook runner broke")
            return real(home, phase, *a, **kw)
        r = self.build(); r.start(); self.work(r)
        with mock.patch.object(session_mod, "run_phase", side_effect=run_phase):
            out = r.rollover("max_age")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["generation"], 1)
        self.assertIn("start hook runner broke", out["problems"][0])
        self.assertTrue(_wait(lambda: self.clients[1].queries))
        self.assertIn("STATE DIGEST", self.clients[1].queries[0]["message"]["content"][0]["text"])
        self.assertNotIn("errored", [e["payload"]["to"] for e in r.events() if e["kind"] == "state"])

    def test_no_digest_at_all_still_closes_the_row_delivered(self):
        from cousin_lib.runner import rollover as rollover_mod
        r = self.build(); r.start(); self.work(r)
        bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch.object(prompt, "state_digest", side_effect=RuntimeError("layer broke")), \
                mock.patch.object(rollover_mod, "degraded_digest", side_effect=bad):
            out = r.rollover("max_age")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["digest"].startswith("none:"), out["digest"])
        self.assertEqual(out["generation"], 1)
        self.assertEqual(r.inbox.get(out["inbox_id"])["outcome"], "delivered")
        self.assertNotIn("errored", [e["payload"]["to"] for e in r.events() if e["kind"] == "state"])
        rec = self.work(r, "after")
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        self.assertEqual(len(self.clients), 2)

    def test_a_stop_that_times_out_in_the_tail_still_closes_the_row_delivered_once(self):
        r = self.build()
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[session]\nstart_hooks = ["sleep 1"]\n')
        r.start(); self.work(r)
        t = threading.Thread(target=r.rollover, args=("max_age",)); t.start()
        self.assertTrue(_wait(lambda: boot.read_generation(self.home) == 1))   # in the start hook
        [flip] = r.inbox.open_rows("flip")
        r.stop(timeout=0.2)                        # the join times out: `stopped` is forced
        self.assertEqual(r.state(), "stopped")
        t.join(10)
        self.assertTrue(_wait(lambda: r.inbox.get(flip["id"])["state"] == "done"))
        self.assertEqual(r.inbox.get(flip["id"])["outcome"], "delivered")
        self.assertEqual(r.inbox.open_rows("flip"), [])   # nothing for the next start to re-run
        self.assertEqual(boot.read_generation(self.home), 1)


class TestDuplicateRows(RolloverCase):
    """Two plain `flip` rows put directly (two processes past put_once's check)."""

    def two_plain_rows(self, r):
        a = r.inbox.put(Item("system", "flip", "max_age", sender="runner"))
        b = r.inbox.put(Item("system", "flip", "max_age", sender="runner"))
        return a, b

    def test_a_plain_duplicate_is_closed_with_the_first_ones_answer(self):
        r = self.build(); r.start(); self.work(r)
        a, b = self.two_plain_rows(r)
        self.assertTrue(_wait(lambda: r.inbox.get(b)["state"] == "done"))
        self.assertEqual(r.inbox.get(b)["outcome"], "delivered")
        self.assertEqual(json.loads(r.inbox.get(b)["detail"])["coalesced_into"], a)
        self.assertEqual((len(self.clients), boot.read_generation(self.home)), (2, 1))

    def test_a_bequest_written_over_a_duplicate_mid_close_is_not_closed(self):
        bequest = "You are about to be reincarnated.\nPut a bequest in the handoff's position."
        r = self.build(handoff_delay=0.3)
        real_open_rows, fired = r.inbox.open_rows, []

        def open_rows(source):
            rows = real_open_rows(source)       # the snapshot _close_duplicates decides on
            if not fired and rows:              # (the first row is closed already: b only)
                fired.append(True)              # then put_once's bequest lands in between
                self.assertTrue(r.inbox.replace_body(rows[0]["id"], bequest))
            return rows
        r.inbox.open_rows = open_rows
        r.start(); self.work(r)
        a, b = self.two_plain_rows(r)
        self.assertTrue(_wait(lambda: r.inbox.get(b)["state"] == "done", timeout=15))
        self.assertTrue(fired)
        row_b = r.inbox.get(b)
        self.assertEqual(row_b["body"], bequest)
        self.assertNotIn("coalesced_into", json.loads(row_b["detail"]))   # its own rollover
        self.assertEqual((len(self.clients), boot.read_generation(self.home)), (3, 2))


class TestPressureTrigger(RolloverCase):
    def test_pressure_rolls_over_once_then_hysteresis_holds(self):
        r = self.build()
        with mock.patch.object(r, "_context_usage", return_value={"percentage": 91.0}):
            r.start()
            self.work(r, "fill the context")
            self.assertTrue(_wait(lambda: len(self.clients) == 2))
            for i in range(3):                     # three more turns at 91%: no second rollover
                self.work(r, "turn %d" % i)
            # the pressure check runs after the row closes, then the turn goes
            # idle: wait for it under the patch, or the last turn checks nothing (#102)
            self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertEqual(len(self.clients), 2)
        events = [e["payload"] for e in r.events()
                  if e["kind"] == "rollover" and e["payload"].get("phase") == "start"]
        self.assertEqual([e["reason"] for e in events], ["context pressure 91%"])

    def test_a_token_only_reading_names_its_tokens_and_the_turn_still_succeeds(self):
        r = self.build()
        near = {"totalTokens": 150_000, "autoCompactThreshold": 155_000}   # no percentage
        with mock.patch.object(r, "_context_usage", return_value=near):
            r.start()
            rec = self.work(r, "fill the context")
            self.assertTrue(_wait(lambda: len(self.clients) == 2))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        events = [e["payload"] for e in r.events()
                  if e["kind"] == "rollover" and e["payload"].get("phase") == "start"]
        self.assertEqual([e["reason"] for e in events], ["context pressure 150000 tokens"])


if __name__ == "__main__":
    unittest.main()
