"""The seams side sessions stand on: an SdkRunner that claims
only some thread kinds, keeps its own session file, says what it is doing
without saying whose words it is reading, and a handoff refused outside
the primary session."""
import json
import os
import time
import unittest

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner import tools
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.sdk import SdkRunner
from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, _wait, assistant, init_msg, result


def _op(body):
    return Item("operator:priya", "chat", body, sender="Priya")


def _peer(body):
    return Item("peer:testa", "chat", body, sender="Testa")


class SeamCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def runner(self, scripts=(), **kw):
        made = {"clients": []}

        def factory(options):
            made["clients"].append(ScriptedClient(options, list(scripts)))
            return made["clients"][-1]
        r = SdkRunner(self.home, client_factory=factory, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r, made

    def state_of(self, r, receipt):
        return r.inbox.get(receipt.inbox_id)["state"]


class TestClaimFilter(SeamCase):
    def test_a_runner_that_excludes_peer_leaves_the_peer_row_queued(self):
        r, _ = self.runner(exclude_kinds=("peer",))
        r.start()
        peer = r.enqueue(_peer("hello from Testa"))
        op = r.enqueue(_op("hello from Priya"))
        self.assertTrue(_wait(lambda: self.state_of(r, op) == "done"))
        time.sleep(0.3)
        self.assertEqual(self.state_of(r, peer), "queued")

    def test_a_runner_for_peer_claims_only_peer_rows(self):
        r, _ = self.runner(session="peer", claim_kinds=("peer",))
        r.start()
        op = r.enqueue(_op("hello from Priya"))
        peer = r.enqueue(_peer("hello from Testa"))
        self.assertTrue(_wait(lambda: self.state_of(r, peer) == "done"))
        time.sleep(0.3)
        self.assertEqual(self.state_of(r, op), "queued")

    def test_the_fold_claims_through_the_same_filter(self):
        r, made = self.runner([[init_msg(), assistant(tool="Bash"), "PAUSE",
                                assistant(text="x"), result()]], exclude_kinds=("peer",))
        calls = []
        real = r.inbox.claim

        def recording(**kw):
            calls.append(kw)
            return real(**kw)

        r.inbox.claim = recording
        r.start()
        first = r.enqueue(_op("first"))
        self.assertTrue(_wait(lambda: made["clients"] and made["clients"][0].paused))
        r.enqueue(_peer("mid-turn peer"))
        second = r.enqueue(_op("second"))
        self.assertTrue(_wait(lambda: len(made["clients"][0].queries) == 2))
        made["clients"][0].resume()
        self.assertTrue(_wait(lambda: self.state_of(r, second) == "done"))
        self.assertEqual(self.state_of(r, first), "done")
        self.assertTrue(any(c.get("limit") == 10 for c in calls), "no fold claim was made")
        self.assertTrue(all(c.get("exclude_kinds") == ("peer",) for c in calls), calls)


class TestOwnSessionFile(SeamCase):
    def test_a_side_session_keeps_its_own_session_file(self):
        r, _ = self.runner([[init_msg(session="s-peer"), assistant(text="hi"),
                             result(session="s-peer")]],
                           session="peer", claim_kinds=("peer",))
        self.assertTrue(r.session_id.startswith("sdk-peer-"))
        r.start()
        peer = r.enqueue(_peer("hello"))
        self.assertTrue(_wait(lambda: self.state_of(r, peer) == "done"))
        r.stop(timeout=5)
        data = self.home / "data"
        self.assertEqual(json.loads((data / "runner-session-peer.json").read_text())["session_id"],
                         "s-peer")
        self.assertFalse((data / "runner-session.json").exists())

    def test_the_primary_keeps_the_file_it_always_had(self):
        r, _ = self.runner([[init_msg(session="s-main"), assistant(text="hi"),
                             result(session="s-main")]])
        self.assertTrue(r.session_id.startswith("sdk-") and not r.session_id.startswith("sdk-primary"))
        r.start()
        op = r.enqueue(_op("hello"))
        self.assertTrue(_wait(lambda: self.state_of(r, op) == "done"))
        r.stop(timeout=5)
        self.assertEqual(json.loads((self.home / "data" / "runner-session.json").read_text())
                         ["session_id"], "s-main")


class TestActivity(SeamCase):
    def test_activity_names_state_kinds_and_start_never_whose_words(self):
        r, made = self.runner([[init_msg(), assistant(tool="Bash"), ("SLOW", 3.0),
                                assistant(text="done"), result()]])
        self.assertEqual(r.activity(), {"state": "idle", "thread_kinds": [], "since": None})
        r.start()
        t0 = time.time()
        receipt = r.enqueue(_op("the quarterly numbers Priya asked about"))
        # the machine moves to running a moment before the turn begins
        self.assertTrue(_wait(lambda: r.activity()["thread_kinds"]))
        now = r.activity()
        self.assertEqual(now["state"], "running")
        self.assertEqual(now["thread_kinds"], ["operator"])
        self.assertLessEqual(abs(now["since"] - t0), 5.0)
        text = json.dumps(now)
        for secret in ("quarterly", "priya", "Priya"):
            self.assertNotIn(secret, text)
        r.interrupt()
        self.assertTrue(_wait(lambda: self.state_of(r, receipt) == "done"))
        self.assertTrue(_wait(lambda: r.activity()["state"] == "idle"))
        self.assertEqual(r.activity()["since"], None)


class TestHandoffIsThePrimarys(HermeticCase):
    def _ctx(self, home, session):
        return tools.ToolContext(home=home, slug="wren", name="Wren", root=home.parent.parent,
                                 turn=Turn(), policy=Policy(), session=session)

    def test_handoff_is_refused_on_a_side_session(self):
        home = temp_home(self)
        text, is_error = tools.call(self._ctx(home, "peer"), "handoff", {
            "position": "p", "next_action": "n", "status": "- [ ] s"})
        self.assertTrue(is_error)
        self.assertIn("primary session", text)
        self.assertFalse((home / "STATUS.md").exists())
        self.assertFalse((home / "data" / "handoff.md").exists())

    def test_the_primary_still_hands_off(self):
        home = temp_home(self)
        text, is_error = tools.call(self._ctx(home, "primary"), "handoff", {
            "position": "p", "next_action": "n", "status": "- [ ] s"})
        self.assertFalse(is_error, text)
        self.assertTrue((home / "data" / "handoff.md").exists())

    def test_a_side_sessions_activity_note_names_its_session(self):
        """The note is the home's; a side session's never passes
        for what the primary is doing."""
        home = temp_home(self)
        for session, expected in (("peer", "[peer session] answering Testa"),
                                  ("primary", "rebuilding the report")):
            text, is_error = tools.call(self._ctx(home, session), "memory", {
                "command": "activity", "text": expected.split("] ")[-1]})
            self.assertFalse(is_error, text)
            note = (home / "data" / "last-activity.txt").read_text()
            self.assertTrue(note.rstrip("\n").endswith(": " + expected), note)

    def test_the_tool_list_is_the_same_in_every_session(self):
        """The cached prefix holds the tools; a side session
        that dropped handoff would cache apart. It is refused, not removed."""
        self.assertIn("handoff", [d["name"] for d in tools.tool_definitions(
            {"tools": {}})])


if __name__ == "__main__":
    unittest.main()
