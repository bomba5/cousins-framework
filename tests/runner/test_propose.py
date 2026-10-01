"""A post-turn memory proposal: the harness asks, so writing a memory does
not depend on the cousin's discipline (spec, "One memory system")."""
import asyncio
import os
import pathlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401 - the store's append folds a summary with it
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner import extract
from cousin_lib.runner.base import priority
from cousin_lib.runner.sdk import SdkRunner
from cousin_lib.runner.session_store import SqliteSessionStore
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result  # noqa: E402

DECIDED = "Decided to keep the ledger in SQLite because the export reads it directly."


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True); (home / "memory").mkdir()
    return home


def _turn(store, sid, n, text, tool_input=None):
    content = [{"type": "text", "text": text}]
    if tool_input is not None:
        content.insert(0, {"type": "tool_use", "id": "tu-%d" % n, "name": extract.MEMORY_TOOL,
                           "input": tool_input})
    entries = [{"type": "user", "uuid": "%s-u%d" % (sid, n),
                "message": {"role": "user", "content": "go"}},
               {"type": "assistant", "uuid": "%s-a%d" % (sid, n),
                "message": {"role": "assistant", "content": content}}]
    asyncio.run(store.append({"project_key": "p", "session_id": sid}, entries))


class TestProposeTurn(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = _home(self); self.store = SqliteSessionStore(self.home)

    def test_a_turn_carrying_a_decision_produces_a_proposal(self):
        _turn(self.store, "sess-0001", 1, DECIDED + " ok.")
        body = extract.propose_turn(self.home, "sess-0001", store=self.store)
        self.assertTrue(body.startswith(extract.PROPOSAL_MARK), body)
        self.assertIn(DECIDED, body)

    def test_a_turn_carrying_none_produces_nothing(self):
        _turn(self.store, "sess-0001", 1, "Looking at it now.")
        self.assertIsNone(extract.propose_turn(self.home, "sess-0001", store=self.store))

    def test_a_conclusion_without_a_decision_is_mined_not_proposed(self):
        """extract.py still mines it as an episode: entry; only a
        sentence carrying a decision word is worth asking about."""
        _turn(self.store, "sess-0001", 1, "The pin floated because the pull-up was missing.")
        self.assertIsNone(extract.propose_turn(self.home, "sess-0001", store=self.store))

    def test_a_turn_that_recorded_it_already_produces_nothing(self):
        _turn(self.store, "sess-0001", 1, DECIDED,
              tool_input={"command": "decide", "topic": "ledger store", "decision": "SQLite",
                          "reasoning": "the export reads it"})
        self.assertIsNone(extract.propose_turn(self.home, "sess-0001", store=self.store))

    def test_a_proposal_turn_never_proposes(self):
        _turn(self.store, "sess-0001", 1, DECIDED)
        body = extract.PROPOSAL_MARK + " Your last turn reached conclusions ..."
        self.assertIsNone(extract.propose_turn(self.home, "sess-0001", store=self.store,
                                               turn_bodies=(body,)))

    def test_a_proposal_turn_is_consumed_so_the_next_turn_does_not_reread_it(self):
        """The proposal's own turn restates the conclusion;
        the plain turn after it must not be proposed about on that text."""
        mark = extract.PROPOSAL_MARK + " Your last turn reached conclusions ..."
        _turn(self.store, "sess-0001", 1, DECIDED)                   # the proposal's own turn
        self.assertIsNone(extract.propose_turn(self.home, "sess-0001", store=self.store,
                                               turn_bodies=(mark,)))
        _turn(self.store, "sess-0001", 2, "Looking at it now.")        # a plain turn, no conclusion
        self.assertIsNone(extract.propose_turn(self.home, "sess-0001", store=self.store))

    def test_a_re_run_proposes_once(self):
        _turn(self.store, "sess-0001", 1, DECIDED)
        self.assertIsNotNone(extract.propose_turn(self.home, "sess-0001", store=self.store))
        self.assertIsNone(extract.propose_turn(self.home, "sess-0001", store=self.store))

    def test_the_cap_holds_over_a_rolling_day(self):
        now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        made = 0
        for n in range(3 * extract.PROPOSAL_CAP):
            sid = "sess-%04d" % n
            _turn(self.store, sid, 1, "Decided to keep ledger %d because audit %d reads it." % (n, n))
            made += extract.propose_turn(self.home, sid, store=self.store, now=now) is not None
        self.assertEqual(made, extract.PROPOSAL_CAP)
        _turn(self.store, "sess-late", 1, DECIDED)
        later = now + timedelta(seconds=extract.WINDOW_S + 1)
        self.assertIsNotNone(extract.propose_turn(self.home, "sess-late", store=self.store, now=later))

    def test_a_broken_store_raises_so_the_caller_can_surface_it(self):
        """None already means "nothing to propose": folding a broken store
        or corrupt state into it would switch proposals off with no
        trace. propose_turn raises; SdkRunner._propose is the
        layer that must never fail a turn, and it catches this."""
        class Broken:
            def entries_after(self, *a, **k):
                raise RuntimeError("store gone")
        with self.assertRaises(RuntimeError):
            extract.propose_turn(self.home, "sess-0001", store=Broken())


class TestPriority(unittest.TestCase):
    def test_a_proposal_waits_behind_every_other_source(self):
        others = [priority(s, "system") for s in ("chat", "reaction", "hook", "boot", "meeting",
                                                  "schedule", "loop")]
        self.assertGreater(priority(extract.PROPOSAL_SOURCE, "system"), max(others))


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestWiring(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="sdk")
        (self.home.parent.parent / "config").mkdir(exist_ok=True)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)

    def _runner(self):
        r = SdkRunner(self.home, client_factory=lambda o: ScriptedClient(
            o, [[init_msg(session="s-1"), assistant(text="ok"), result(session="s-1")]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def _proposals(self, r):
        return [e["payload"] for e in r.events() if e["kind"] == "propose"]

    def test_a_proposal_is_queued_and_its_own_turn_passes_its_body(self):
        body = extract.PROPOSAL_MARK + " keep this?"
        calls = []

        def fake(home, sid, *, store, turn_bodies=(), now=None):
            calls.append(tuple(turn_bodies))
            return body if len(calls) == 1 else None
        with mock.patch.object(extract, "propose_turn", fake):
            r = self._runner(); r.start()
            rec = r.enqueue(Item("operator:priya", "chat", "start", sender="Priya"))
            self.assertTrue(_wait(lambda: len(calls) >= 2))
        pid = self._proposals(r)[0]["proposal"]
        row = r.inbox.get(pid)
        self.assertEqual((row["source"], row["thread_id"], row["body"]), ("propose", "system", body))
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "done")
        self.assertIn(body, calls[1])            # the proposal's own turn is told it is one

    def test_a_raising_proposal_never_fails_the_turn(self):
        with mock.patch.object(extract, "propose_turn", side_effect=RuntimeError("boom")):
            r = self._runner(); r.start()
            rec = r.enqueue(Item("operator:priya", "chat", "start", sender="Priya"))
            self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
            self.assertTrue(_wait(lambda: any("boom" in str(p.get("error")) for p in self._proposals(r))))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")

    def test_a_corrupt_proposals_state_is_a_propose_error_not_a_silent_null(self):
        """proposals.json in a shape this module never writes (an
        int where the cap's list belongs) must show up as the `propose`
        event's error, not a quiet {"proposal": null} indistinguishable
        from "nothing to propose". ScriptedClient never writes to the
        session store (that is the real SDK's job), so the turn a
        conclusion is proposed about is seeded into the store directly,
        the same way TestProposeTurn does."""
        (self.home / "data" / "proposals.json").write_text('{"sent": 5}')
        r = self._runner()
        asyncio.run(r.session_store.append(
            {"project_key": "p", "session_id": "s-1"},
            [{"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
             {"type": "assistant", "uuid": "a1",
              "message": {"role": "assistant", "content": [{"type": "text", "text": DECIDED}]}}]))
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "start", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
        self.assertTrue(_wait(lambda: self._proposals(r)))
        payload = self._proposals(r)[0]
        self.assertIsNone(payload["proposal"])
        self.assertTrue(payload.get("error"))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")


if __name__ == "__main__":
    unittest.main()
