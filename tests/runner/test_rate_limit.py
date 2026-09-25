"""A RateLimitEvent is the rate_limited state; nothing is claimed while it holds."""
import json
import os
import time
import unittest
from unittest import mock

try:
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result


def _limit(status, resets_in=1.5, overage=None):
    return RateLimitEvent(rate_limit_info=RateLimitInfo(status=status,
                          resets_at=int(time.time() + resets_in) + 1, overage_status=overage),
                          uuid="rl-1", session_id="s-1")


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestRateLimited(HermeticCase):
    def build(self, first):
        home = temp_home(self)
        root = home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}); p.start(); self.addCleanup(p.stop)
        scripts = [first] + [[init_msg(), assistant(text="ok"), result()] for _ in range(3)]
        self.client = None

        def factory(options):
            self.client = ScriptedClient(options, scripts)
            return self.client
        r = SdkRunner(home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def states(self, r):
        return [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]

    def test_rejected_moves_to_rate_limited_and_back_when_the_window_reopens(self):
        r = self.build([init_msg(), _limit("rejected"), result(is_error=True)])
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "rate_limited"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered", 15))
        st = self.states(r)
        i = st.index("rate_limited")
        self.assertIn("idle", st[i + 1:])
        payload = [e["payload"] for e in r.events() if e["kind"] == "rate_limit"][0]
        self.assertEqual(payload["status"], "rejected"); self.assertIsNotNone(payload["resets_at"])

    def test_no_row_is_claimed_while_limited(self):
        r = self.build([init_msg(), _limit("rejected", resets_in=2.0), result(is_error=True)])
        r.start()
        r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "rate_limited"))
        rec = r.enqueue(Item("peer:toki", "chat", "second", sender="Toki"))
        time.sleep(0.5)
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "queued")
        self.assertEqual(r.state(), "rate_limited")
        # ...and it is claimed once the window reopens, never before it
        until = [e["payload"]["resets_at"] for e in r.events() if e["kind"] == "rate_limit"][0]
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered", 15))
        self.assertGreaterEqual(r.inbox.get(rec.inbox_id)["claimed_at"], until)

    def test_the_limited_row_is_requeued_not_failed_and_the_repeat_is_said(self):
        r = self.build([init_msg(), _limit("rejected", resets_in=5.0), result(is_error=True)])
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        # the result is appended, then the row goes back to the queue (#87, #102)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "queued"
                              and r.state() == "rate_limited"
                              and any(e["kind"] == "result" and e["payload"].get("requeued")
                                      for e in r.events())))
        self.assertIsNone(r.inbox.get(rec.inbox_id)["outcome"])
        requeued = [e["payload"] for e in r.events()
                    if e["kind"] == "result" and e["payload"].get("requeued")]
        self.assertTrue(requeued[0]["repeat_in_transcript"])

    def test_rejected_with_overage_allowed_does_not_block(self):
        r = self.build([init_msg(), _limit("rejected", overage="allowed"), assistant(text="ok"),
                        result()])
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertNotIn("rate_limited", self.states(r))
        payload = [e["payload"] for e in r.events() if e["kind"] == "rate_limit"][0]
        self.assertTrue(payload["overage"])

    def test_a_limit_during_the_handoff_postpones_the_rollover_not_an_emergency(self):
        from cousin_lib.runner import tools
        args = {"position": "p", "next_action": "n", "status": "- s"}
        holder = {}
        r = self.build([init_msg(), assistant(text="work"), result()])
        # generation 1: a work turn, a handoff turn that is rate limited, then one that answers
        r.client_factory = self._factory_with([
            [init_msg(), assistant(text="work"), result()],
            [init_msg(), _limit("rejected", resets_in=1.0), result(is_error=True)],
            [init_msg(), ("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", args)),
             assistant(text="handed off"), result()]])
        holder["r"] = r
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
        out = r.rollover("max_age")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["handoff"], "clean")                      # not an emergency
        phases = [e["payload"].get("phase") for e in r.events() if e["kind"] == "rollover"]
        self.assertIn("postponed", phases)
        self.assertNotIn("degraded_state: true", (r.home / "data" / "handoff.md").read_text())

    def test_a_lost_resume_in_a_limited_handoff_leaves_the_start_to_the_rollover(self):
        # a restart whose handoff turn names another session (a lost resume) AND is
        # rate limited: the rollover is postponed and the lost resume goes with
        # it, so the start hooks and the digest run once, from the rollover
        from cousin_lib.runner import rollover, tools
        args = {"position": "p", "next_action": "n", "status": "- s"}
        holder = {}
        r = self.build([init_msg(), assistant(text="ok"), result()])
        (r.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-saved", "lane": "login", "generation": 0, "updated": 0}))
        (r.home / "STATUS.md").write_text("## Open loops\n- carry this\n")
        with open(r.home / "cousin.toml", "a") as fh:
            fh.write('\n[session]\nstart_hooks = ["echo start >> %s/hooks.log"]\n' % r.home)
        # client 0 (the resume, answered as another session): the handoff turn,
        # limited, then the handoff answered; client 1: the new session
        r.client_factory = self._factory_with([
            [init_msg(session="s-new"), _limit("rejected", resets_in=1.0),
             result(is_error=True, session="s-new")],
            [init_msg(session="s-new"),
             ("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", args)),
             assistant(text="handed off"), result(session="s-new")]])
        holder["r"] = r
        rollover.put_once(r.inbox, r.home, "max_age")
        r.start()
        self.assertTrue(_wait(lambda: any(e["payload"].get("phase") == "done"
                                          for e in r.events() if e["kind"] == "rollover"), 15))
        phases = [e["payload"].get("phase") for e in r.events() if e["kind"] == "rollover"]
        self.assertIn("postponed", phases)
        rows = [row for row in (r.inbox.get(i) for i in range(1, 40)) if row]
        digests = [row for row in rows if "STATE DIGEST" in (row["body"] or "")]
        self.assertEqual(len(digests), 1)
        self.assertTrue(_wait(lambda: r.inbox.get(digests[0]["id"])["outcome"] == "delivered"))
        self.assertEqual([row for row in rows if row["state"] == "claimed"
                          and row["id"] != digests[0]["id"]], [])
        self.assertEqual((r.home / "hooks.log").read_text().split(), ["start"])
        done = [e["payload"] for e in r.events()
                if e["kind"] == "rollover" and e["payload"].get("phase") == "done"][0]
        self.assertEqual(done["handoff"], "clean")

    def _factory_with(self, gen1):
        made = []

        def factory(options):
            scripts = gen1 if not made else [[init_msg(), assistant(text="ok"), result()]] * 4
            made.append(ScriptedClient(options, scripts))
            return made[-1]
        return factory

    def test_the_rollover_digest_waits_for_the_window(self):
        # a limit during a handoff that still answers: the rollover is clean,
        # and the new session's digest, claimed by id, waits for the window
        from cousin_lib.runner import tools
        args = {"position": "p", "next_action": "n", "status": "- s"}
        holder = {}
        r = self.build([init_msg(), assistant(text="work"), result()])
        r.client_factory = self._factory_with([
            [init_msg(), assistant(text="work"), result()],
            [init_msg(), _limit("rejected", resets_in=2.0),
             ("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", args)),
             assistant(text="handed off"), result()]])
        holder["r"] = r
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
        out = r.rollover("max_age")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["handoff"], "clean")
        until = [e["payload"]["resets_at"] for e in r.events() if e["kind"] == "rate_limit"][0]
        digest = json.loads(r.inbox.get(out["inbox_id"])["detail"])["digest"]
        self.assertEqual(digest, "built")
        boot_rows = lambda: [row for row in (r.inbox.get(i) for i in range(1, 20))  # noqa: E731
                             if row and row["source"] == "boot"]
        self.assertTrue(_wait(lambda: boot_rows() and boot_rows()[-1]["state"] == "done", 15))
        self.assertGreaterEqual(boot_rows()[-1]["claimed_at"], until)

    def test_a_fresh_starts_digest_waits_for_the_window(self):
        # a resume that comes back as a new session, in a turn that hit the
        # limit: the fresh start's digest, claimed by id, waits for the window
        home = temp_home(self)
        root = home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}); p.start(); self.addCleanup(p.stop)
        (home / "data").mkdir(exist_ok=True)
        (home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-old", "generation": 0, "updated": 0}))
        scripts = [[init_msg(session="s-new"), _limit("rejected", resets_in=2.0),
                    result(is_error=True, session="s-new")]] \
            + [[init_msg(session="s-new"), assistant(text="ok"), result(session="s-new")]] * 3
        r = SdkRunner(home, client_factory=lambda options: ScriptedClient(options, scripts))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered", 15))
        kinds = [(e["kind"], e["payload"].get("subtype")) for e in r.events()]
        self.assertIn(("system", "resume_failed"), kinds)
        until = [e["payload"]["resets_at"] for e in r.events() if e["kind"] == "rate_limit"][0]
        boot_rows = [row for row in (r.inbox.get(i) for i in range(1, 20))
                     if row and row["source"] == "boot"]
        self.assertEqual(len(boot_rows), 1)
        self.assertGreaterEqual(boot_rows[0]["claimed_at"], until)

    def test_a_drained_result_records_its_usage(self):
        # a failed turn's cost is still a cost
        r = self.build([init_msg(), "HANG", assistant(text="late"), result()])
        r.idle_timeout_s = 0.5
        r.start()
        r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" and e["payload"].get("drained")
                                          for e in r.events())))
        drained = next(i for i, e in enumerate(r.events())
                       if e["kind"] == "result" and e["payload"].get("drained"))
        self.assertTrue(_wait(lambda: "usage" in [e["kind"] for e in r.events()][drained:]))

    def test_a_warning_is_recorded_and_changes_nothing(self):
        r = self.build([init_msg(), _limit("allowed_warning"), assistant(text="ok"), result()])
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertNotIn("rate_limited", self.states(r))
        self.assertTrue(any(e["kind"] == "rate_limit" for e in r.events()))


if __name__ == "__main__":
    unittest.main()
