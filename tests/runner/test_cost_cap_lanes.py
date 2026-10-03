"""The daily cost cap on the sdk and opencode lanes: a refused row never
reaches the model, and an operator's over the cap carries the runner note
in the prompt text the model reads. Invented cast only."""
import unittest
import uuid

from cousin_lib import usage
from cousin_lib.delivery import Item
from cousin_lib.runner import cost_cap
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_opencode import OpencodeCase, _wait

NOTE = "[runner] daily cost cap reached: $3.00 of $2.00 today (UTC)"
DETAIL = "daily cost cap reached: spent $3.00 of $2.00 today (UTC)"


def _over(home, cap=2, spent=3.0):
    toml = (home / "cousin.toml").read_text()
    (home / "cousin.toml").write_text(toml.replace(
        "[agent]\n", "[agent]\n%s = %s\n" % (cost_cap.KEY, cap), 1))
    usage.record(home, client_id=uuid.uuid4().hex, session_id="s",
                 result={"usage": {"input_tokens": 10, "output_tokens": 5},
                         "total_cost_usd": spent}, lane="login")


def _caps(r):
    return [e["payload"] for e in r.events() if e["kind"] == "cap"]


class TestOpencodeLane(OpencodeCase):
    def test_a_loop_is_refused_and_an_operator_runs_with_the_note(self):
        home = self.home()
        _over(home)
        r = self.started(self.runner(home=home))
        loop = r.enqueue(Item("loop:heartbeat", "loop", "tick", sender="loop"))
        self.assertTrue(_wait(lambda: self.outcome(r, loop) is not None))
        self.assertEqual(self.outcome(r, loop), "failed")
        self.assertEqual(r.inbox.get(loop.inbox_id)["detail"], DETAIL)
        self.assertEqual(self.prompts(), [])                   # never reached the model
        op = r.enqueue(Item("operator:ana", "chat", "status?", sender="ana"))
        self.assertTrue(_wait(lambda: self.settled(r, op) is not None, 8))
        self.assertEqual(self.outcome(r, op), "delivered")
        text = self.prompts()[0]["body"]["parts"][0]["text"]
        self.assertTrue(text.startswith("[operator:ana] chat from ana"), text)
        self.assertIn(NOTE, text)
        self.assertEqual([("refused" in c, c.get("allowed")) for c in _caps(r)],
                         [(True, None), (False, "person")])


class TestSdkLane(HermeticCase):
    def setUp(self):
        super().setUp()
        try:
            from tests.runner import test_sdk
        except unittest.SkipTest as skip:
            self.skipTest(str(skip))
        self.t = test_sdk
        self.home = temp_home(self)

    def _runner(self, scripts):
        from cousin_lib.runner.sdk import SdkRunner
        made = {}

        def factory(options):
            made["client"] = self.t.ScriptedClient(options, scripts)
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r, made

    def test_a_peer_is_refused_and_a_person_runs_with_the_note(self):
        t = self.t
        _over(self.home)
        r, made = self._runner([[t.init_msg("none"), t.assistant(text="ok"), t.result()]])
        r.start()
        peer = r.enqueue(Item("peer:kestrel", "chat", "ping", sender="Kestrel"))
        self.assertTrue(t._wait(lambda: r.inbox.get(peer.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(peer.inbox_id)["outcome"], "failed")
        self.assertEqual(r.inbox.get(peer.inbox_id)["detail"], DETAIL)
        person = r.enqueue(Item("person:priya", "chat", "hello", sender="Priya"))
        self.assertTrue(t._wait(lambda: r.inbox.get(person.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(person.inbox_id)["outcome"], "delivered")
        queries = made["client"].queries
        self.assertEqual(len(queries), 1)                       # the person's only
        text = queries[0]["message"]["content"][0]["text"]
        self.assertTrue(text.startswith("[person:priya] chat from Priya"), text)
        self.assertIn("hello", text)
        self.assertIn(NOTE, text)
        starts = [e["payload"] for e in r.events() if e["kind"] == "turn_start"]
        self.assertEqual([s["bodies"] for s in starts], [["hello"]])
        self.assertEqual([("refused" in c, c.get("allowed")) for c in _caps(r)],
                         [(True, None), (False, "person")])


if __name__ == "__main__":
    unittest.main()
