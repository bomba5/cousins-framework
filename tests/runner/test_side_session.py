"""A side session (phase 8): one thread kind's session of its own. It
claims only its kind, tells its first turn who it is and what the primary
is doing, never proposes, and starts over (never rolls the generation)
on pressure or when the primary's generation moves."""
import asyncio
import json
import pathlib
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib import boot
from cousin_lib.delivery import Item
from cousin_lib.runner import envelope, extract, sdk, sessions, wake
from cousin_lib.runner.base import INTERRUPT
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import (ScriptedClient, _wait, asked_resume, assistant, init_msg,
                                   result)

BUSY = {"state": "running", "thread_kinds": ["operator"], "since": time.time() - 120}


def _peer(body):
    return Item("peer:testa", "chat", body, sender="Testa")


def _turn(session):
    return [init_msg(session=session), assistant(text="answered"), result(session=session)]


def _text(query):
    return query["message"]["content"][0]["text"]


class SideCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.clients = []

    def side(self, per_client=None, *, activity=BUSY, fail=(), login=(), runner=None, **kw):
        """A peer side session. Client n (0-based) plays per_client[n] (one
        CLI turn per script); a connect of client n raises when n is in `fail`,
        and raises a logged-out error when n is in `login`."""
        per_client = per_client or [[_turn("s-1") for _ in range(4)]]

        def factory(options):
            n = len(self.clients)
            client = ScriptedClient(options, per_client[n] if n < len(per_client) else [])
            if n in fail or n in login:
                error = "Not logged in - Please run /login" if n in login \
                    else "the CLI did not start"

                async def refuse(prompt=None):
                    raise OSError(error)
                client.connect = refuse
            self.clients.append(client)
            return client
        cls = runner or sessions.SideSession
        r = cls(self.home, kind="peer", client_factory=factory,
                primary_activity=lambda: activity, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def done(self, r, receipt, timeout=5.0):
        return _wait(lambda: r.inbox.get(receipt.inbox_id)["state"] == "done", timeout)


class TestClaims(SideCase):
    def test_a_side_session_answers_only_its_kind(self):
        r = self.side()
        r.start()
        op = r.enqueue(Item("operator:priya", "chat", "for the primary", sender="Priya"))
        peer = r.enqueue(_peer("for the side"))
        self.assertTrue(self.done(r, peer))
        time.sleep(0.3)
        self.assertEqual(r.inbox.get(op.inbox_id)["state"], "queued")

    def test_it_polls_and_leaves_the_wake_socket_to_the_primary(self):
        r = self.side()
        r.start()
        peer = r.enqueue(_peer("hello"))
        self.assertTrue(self.done(r, peer))
        self.assertFalse(wake.socket_path(self.home).exists())

    def test_operator_and_system_get_no_side_session(self):
        for kind in ("operator", "system", "nonsense"):
            with self.subTest(kind=kind), self.assertRaises(sessions.SessionsError):
                sessions.SideSession(self.home, kind=kind, client_factory=lambda o: None)

    def test_the_session_word_is_the_same_in_both_modules(self):
        self.assertEqual(sdk.PRIMARY, sessions.PRIMARY)


class TestDigest(SideCase):
    def test_the_first_turn_carries_the_side_digest_and_only_the_first(self):
        r = self.side()
        r.start()
        a = r.enqueue(_peer("first question"))
        self.assertTrue(self.done(r, a))
        b = r.enqueue(_peer("second question"))
        self.assertTrue(self.done(r, b))
        first, second = (_text(q) for q in self.clients[0].queries)
        self.assertIn("first question", first)
        self.assertIn(envelope.CONTEXT_MARK, first)
        self.assertIn("SIDE SESSION: peer threads of wren", first)
        self.assertIn("Primary session: running, in a turn on operator threads", first)
        self.assertNotIn("SIDE SESSION", second)

    def test_the_rows_own_context_is_kept_after_the_digest(self):
        r = self.side()
        r.start()
        a = r.enqueue(Item("peer:testa", "chat", "hi", sender="Testa", context="[fw-recall] x"))
        self.assertTrue(self.done(r, a))
        text = _text(self.clients[0].queries[0])
        self.assertLess(text.index("SIDE SESSION"), text.index("[fw-recall] x"))

    def test_the_inbox_row_itself_is_not_rewritten(self):
        r = self.side()
        r.start()
        a = r.enqueue(_peer("hello"))
        self.assertTrue(self.done(r, a))
        self.assertEqual(r.inbox.get(a.inbox_id)["context"], "")


class TestNoProposals(SideCase):
    def test_a_side_session_never_proposes(self):
        with mock.patch.object(extract, "propose_turn", return_value="[memory proposal] keep it"):
            r = self.side()
            r.start()
            a = r.enqueue(_peer("we decided to keep the ledger monthly"))
            self.assertTrue(self.done(r, a))
            time.sleep(0.3)
            self.assertEqual(r.inbox.open_rows("propose"), [])
            self.assertFalse(any(e["kind"] == "propose" for e in r.events()))

    def test_the_same_turn_on_a_plain_runner_does_propose(self):
        """The control: the patch reaches the runner's proposal step."""
        with mock.patch.object(extract, "propose_turn", return_value="[memory proposal] keep it"):
            r = self.side(runner=_PlainPeer)
            r.start()
            a = r.enqueue(_peer("we decided to keep the ledger monthly"))
            self.assertTrue(self.done(r, a))
            self.assertTrue(_wait(lambda: r.inbox.open_rows("propose")))


class _PlainPeer(sdk.SdkRunner):
    def __init__(self, home, *, kind, primary_activity=None, **kw):
        super().__init__(home, session=kind, claim_kinds=(kind,), **kw)


class TestReset(SideCase):
    def test_pressure_resets_the_side_session_not_the_generation(self):
        r = self.side([[_turn("s-1")], [_turn("s-2")]])
        r.start()
        a = r.enqueue(_peer("one"))
        self.assertTrue(self.done(r, a))
        r._request_rollover("context pressure 85%")
        b = r.enqueue(_peer("two"))
        self.assertTrue(self.done(r, b))
        self.assertEqual(len(self.clients), 2)
        self.assertIsNone(asked_resume(self.clients[1].options))
        self.assertIn("SIDE SESSION", _text(self.clients[1].queries[0]))
        self.assertEqual(boot.read_generation(self.home), 0)
        self.assertEqual(r.inbox.open_rows("flip"), [])
        self.assertFalse((self.home / "data" / "handoff.md").exists())
        self.assertTrue(_wait(lambda: json.loads(
            (self.home / "data" / "runner-session-peer.json").read_text())["session_id"] == "s-2"))
        phases = [e["payload"]["phase"] for e in r.events() if e["kind"] == "rollover"]
        self.assertEqual(phases, ["requested", "start", "done"])

    def test_the_primarys_rollover_resets_it_at_its_next_turn(self):
        r = self.side([[_turn("s-1")], [_turn("s-2")]])
        r.start()
        a = r.enqueue(_peer("one"))
        self.assertTrue(self.done(r, a))
        boot.bump_generation(self.home)
        b = r.enqueue(_peer("two"))
        self.assertTrue(self.done(r, b))
        self.assertEqual(len(self.clients), 2)
        start = next(e["payload"] for e in r.events()
                     if e["kind"] == "rollover" and e["payload"]["phase"] == "start")
        self.assertEqual(start["reason"], "the primary session moved to generation 1")

    def test_a_session_saved_in_an_older_generation_starts_over(self):
        (self.home / "data" / "runner-session-peer.json").write_text(json.dumps(
            {"session_id": "s-old", "lane": "unknown", "generation": 0}))
        (self.home / "data" / "generation.txt").write_text("1")
        r = self.side([[_turn("s-old")], [_turn("s-2")]])
        r.start()
        a = r.enqueue(_peer("hello"))
        self.assertTrue(self.done(r, a))
        self.assertEqual(asked_resume(self.clients[0].options), "s-old")
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.clients[0].queries, [])     # nothing ran on the old session
        self.assertIn("SIDE SESSION", _text(self.clients[1].queries[0]))

    def test_a_session_saved_in_this_generation_is_resumed_without_a_digest(self):
        (self.home / "data" / "runner-session-peer.json").write_text(json.dumps(
            {"session_id": "s-old", "lane": "unknown", "generation": 0}))
        r = self.side([[_turn("s-old")]])
        r.start()
        a = r.enqueue(_peer("hello again"))
        self.assertTrue(self.done(r, a))
        self.assertEqual(len(self.clients), 1)
        self.assertNotIn("SIDE SESSION", _text(self.clients[0].queries[0]))

    def test_a_reset_that_cannot_start_a_new_session_goes_back_to_the_old_one(self):
        r = self.side([[_turn("s-1")], [], [_turn("s-1")]], fail=(1,))
        r.start()
        a = r.enqueue(_peer("one"))
        self.assertTrue(self.done(r, a))
        r._request_rollover("context pressure 85%")
        b = r.enqueue(_peer("two"))
        self.assertTrue(self.done(r, b))
        self.assertEqual(len(self.clients), 3)
        self.assertEqual(asked_resume(self.clients[2].options), "s-1")
        failed = [e["payload"] for e in r.events()
                  if e["kind"] == "rollover" and e["payload"]["phase"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")

    def test_a_reset_with_no_session_at_all_gives_up_and_the_row_waits(self):
        r = self.side([[_turn("s-1")]], fail=(1, 2))
        r.start()
        a = r.enqueue(_peer("one"))
        self.assertTrue(self.done(r, a))
        r._request_rollover("context pressure 85%")
        b = r.enqueue(_peer("two"))
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        self.assertIn("no session after a reset", r.fatal)
        self.assertEqual(r.inbox.get(b.inbox_id)["state"], "queued")


    def test_a_fallback_refused_for_the_login_waits_for_it_never_fatal(self):
        """R15: a login is never fatal. The new session fails for another
        reason, the fallback to the old one is refused for the login: the
        side session waits for the login, back on the old session, and its
        worker lives on."""
        from cousin_lib import accounts
        from cousin_lib.runner import auth
        for patch in (mock.patch.object(accounts, "status", return_value={"loggedIn": False}),
                      mock.patch.object(auth, "credential_mark", return_value=("m", 1))):
            patch.start()
            self.addCleanup(patch.stop)
        r = self.side([[_turn("s-1")]], fail=(1,), login=(2,))
        waiting = __import__("threading").Event()
        real_await = r._await_login

        async def await_login():
            waiting.set()
            return await real_await()
        r._await_login = await_login
        r.start()
        a = r.enqueue(_peer("one"))
        self.assertTrue(self.done(r, a))
        r._request_rollover("context pressure 85%")
        b = r.enqueue(_peer("two"))
        self.assertTrue(_wait(lambda: waiting.is_set() or not r.worker_alive(), timeout=15))
        self.assertIsNone(r.fatal)
        self.assertTrue(r.login_required())
        self.assertEqual(r.state(), "errored")
        self.assertEqual(r._resume_id, "s-1")        # the login retry resumes it
        self.assertTrue(r.worker_alive())
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "queued"))

    def test_a_generation_read_error_in_a_fresh_start_keeps_the_digest_due(self):
        """R6: the side digest is due from the moment a fresh session exists.
        A transient error reading the generation in _start_fresh must not
        leave the new session's first turn without it."""
        r = self.side([[_turn("s-1")], [_turn("s-2")]])
        armed = []
        real_connect, real_read = r._connect, boot.read_generation

        async def connect(**kw):
            ok = await real_connect(**kw)
            if ok and kw.get("why") == "side reset":
                armed.append(True)        # the next generation read is _start_fresh's
            return ok

        def read_generation(home):
            if armed:
                armed.clear()
                raise PermissionError("generation.txt: denied for a moment")
            return real_read(home)

        r._connect = connect
        with mock.patch.object(boot, "read_generation", side_effect=read_generation):
            r.start()
            a = r.enqueue(_peer("one"))
            self.assertTrue(self.done(r, a))
            r._request_rollover("context pressure 85%")
            b = r.enqueue(_peer("two"))
            self.assertTrue(self.done(r, b, timeout=15))
        self.assertTrue(any(e["kind"] == "error" and "denied for a moment" in e["payload"]["error"]
                            for e in r.events()))
        self.assertEqual(len(self.clients), 2)
        self.assertIn("SIDE SESSION", _text(self.clients[1].queries[0]))
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")

class TestStreamHead(SideCase):
    """The P8-2 rule: a side session's stream is headed `side_session`, never
    `runner`, so a reader of the primary's stream never takes it."""

    def test_the_first_event_of_a_side_stream_is_side_session(self):
        r = self.side()
        with open(r.stream.path, encoding="utf-8") as fh:
            head = json.loads(fh.readline())
        self.assertEqual(head["kind"], "side_session")
        self.assertEqual(head["payload"], {"kind": "peer", "id": r.session_id,
                                           "thread": "peer:*"})
        self.assertEqual(r.stream.path.name, "%s.jsonl" % r.session_id)
        self.assertTrue(r.session_id.startswith("sdk-peer-"))

    def test_a_bare_kind_names_its_thread_bare(self):
        r = sessions.SideSession(self.home, kind="schedule", client_factory=lambda o: None)
        with open(r.stream.path, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["payload"]["thread"], "schedule")

    def test_side_streams_lists_side_heads_only_newest_first(self):
        old = self.side()
        time.sleep(0.02)
        new = self.side()
        primary = sdk.SdkRunner(self.home, client_factory=lambda o: None)
        primary.stream.append("runner", {"kind": "sdk"})
        listed = sessions.side_streams(self.home)
        self.assertEqual(list(listed), ["peer"])
        self.assertEqual(listed["peer"], [new.stream.path, old.stream.path])


class TestBoundaryNeverStrandsARow(SideCase):
    """Review I1: a claimed row the boundary cannot run goes back to the queue."""

    def test_an_unreadable_generation_file_requeues_the_row_and_says_why(self):
        r = self.side()
        r.start()
        a = r.enqueue(_peer("one"))
        self.assertTrue(self.done(r, a))
        with mock.patch.object(boot, "read_generation", side_effect=PermissionError("denied")):
            b = r.enqueue(_peer("two"))
            self.assertTrue(_wait(lambda: any(
                e["kind"] == "error" and "PermissionError" in e["payload"]["error"]
                for e in r.events())))
            row = r.inbox.get(b.inbox_id)
            self.assertIn(row["state"], ("queued", "claimed"))
            self.assertIsNone(row["outcome"])
        self.assertTrue(self.done(r, b))
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")

    def test_a_reset_due_while_stopping_gives_the_row_back_and_starts_no_cli(self):
        r = self.side()
        r._request_rollover("context pressure 85%")
        r._stop.set()
        receipt = r.enqueue(_peer("hello"))
        row = r.inbox.claim(kinds=("peer",), claimant=r.session_id)[0]
        self.assertFalse(asyncio.run(r._boundary(row)))
        self.assertEqual(r.inbox.get(receipt.inbox_id)["state"], "queued")
        self.assertEqual(self.clients, [])

    def test_a_reset_that_raises_leaves_the_machine_idle_and_the_reason_due(self):
        r = self.side()
        receipt = r.enqueue(_peer("hello"))
        row = r.inbox.claim(kinds=("peer",), claimant=r.session_id)[0]
        r._request_rollover("context pressure 85%")
        with mock.patch.object(r, "_mine", side_effect=OSError("disk gone")):
            self.assertFalse(asyncio.run(r._boundary(row)))
        self.assertEqual(r.state(), "idle")
        self.assertEqual(r._reset_due, "context pressure 85%")
        self.assertEqual(r.inbox.get(receipt.inbox_id)["state"], "queued")


class TestInterruptRowIsThePrimarys(SideCase):
    """Round 2 review N1: the console's interrupt row rides `system`, the
    primary's. A side session in a live turn must leave it for the primary,
    however long the primary is held off (a backoff, a rate-limit or login
    wait, a rollover)."""

    def test_a_live_side_turn_leaves_the_interrupt_row_to_the_primary(self):
        held = {"on": True}
        primary = sdk.SdkRunner(self.home, exclude_kinds=("peer",), client_factory=lambda o:
                                ScriptedClient(o, [[init_msg(session="s-main"),
                                                    assistant(tool="Bash"),
                                                    "WAIT_FOR_INTERRUPT"]]))
        self.addCleanup(lambda: primary.stop(timeout=5))
        real_take = primary._take_interrupts

        async def gated():
            if not held["on"]:
                await real_take()
        primary._take_interrupts = gated
        r = self.side([[[init_msg(session="s-peer"), assistant(tool="Bash"), ("SLOW", 4.0),
                         assistant(text="answered"), result(session="s-peer")]]])
        folds = []
        real_claim = r.inbox.claim

        def counting(**kw):
            if kw.get("limit") == 10:
                folds.append(1)                  # one poll of the live side turn
            return real_claim(**kw)
        r.inbox.claim = counting
        primary.start()
        r.start()
        op = primary.enqueue(Item("operator:priya", "chat", "a long task", sender="Priya"))
        self.assertTrue(_wait(lambda: primary.activity()["thread_kinds"] == ["operator"], 20))
        peer = r.enqueue(_peer("hello"))
        self.assertTrue(_wait(lambda: r.activity()["thread_kinds"] == ["peer"], 20))
        stop = r.enqueue(Item("system", INTERRUPT, "stop", sender="Priya"))
        seen = len(folds)
        # several polls of the live side turn after the row is in (or the row
        # closed, the failure), never a fixed sleep
        _wait(lambda: len(folds) >= seen + 3
              or r.inbox.get(stop.inbox_id)["outcome"] is not None)
        # judged by the outcome and the side client, never by a state: the
        # primary's own claim can make the row read `claimed` for an instant
        self.assertIsNone(r.inbox.get(stop.inbox_id)["outcome"])
        self.assertEqual(self.clients[0].interrupts, 0)
        self.assertGreaterEqual(len(folds), seen + 3, "the side turn stopped polling")
        held["on"] = False
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "done", 20))
        row = r.inbox.get(stop.inbox_id)
        self.assertEqual((row["outcome"], row["claimant"]), ("delivered", primary.session_id))
        self.assertTrue(_wait(lambda: r.inbox.get(op.inbox_id)["state"] == "done", 20))
        self.assertTrue(self.done(r, peer, timeout=20))
        self.assertFalse(any(e["kind"] == "result" and e["payload"].get("interrupted")
                             for e in r.events()))


    def test_a_side_boundary_and_fold_never_claim_the_primarys_interrupt_row(self):
        """Phase 5's Task 3 review: `takes_interrupts` gates only the live-turn
        path. The turn BOUNDARY claim (it closes an interrupt row failed, "no
        turn was running") and the FOLD claim (it requeues one) take whatever
        the claim filter lets through; a side session's filter is its own
        kind, so the primary's `system` interrupt row is never among them."""
        held = {"on": True}
        primary = sdk.SdkRunner(self.home, exclude_kinds=("peer",), client_factory=lambda o:
                                ScriptedClient(o, [[init_msg(session="s-main"),
                                                    assistant(tool="Bash"),
                                                    "WAIT_FOR_INTERRUPT"]]))
        self.addCleanup(lambda: primary.stop(timeout=5))
        real_take = primary._take_interrupts

        async def gated():
            if not held["on"]:
                await real_take()
        primary._take_interrupts = gated
        r = self.side([[[init_msg(session="s-peer"), assistant(tool="Bash"), ("SLOW", 1.0),
                         assistant(text="one"), result(session="s-peer")],
                        _turn("s-peer"), _turn("s-peer")]])
        claims = []
        real_claim = r.inbox.claim

        def recording(**kw):
            rows = real_claim(**kw)
            claims.append((kw.get("limit"), [row["source"] for row in rows]))
            return rows
        r.inbox.claim = recording
        primary.start()
        op = primary.enqueue(Item("operator:priya", "chat", "a long task", sender="Priya"))
        self.assertTrue(_wait(lambda: primary.activity()["thread_kinds"] == ["operator"], 20))
        stop = r.enqueue(Item("system", INTERRUPT, "stop", sender="Priya"))
        r.start()
        first = r.enqueue(_peer("one"))       # a live side turn: it folds every poll
        # a first side turn builds its digest and runs a 1 s turn: a loaded
        # host has taken over 5 s (one in 70 runs), so these waits are longer
        self.assertTrue(self.done(r, first, timeout=20))
        for body in ("two", "three"):         # and two more boundaries
            self.assertTrue(self.done(r, r.enqueue(_peer(body)), timeout=20))
        self.assertTrue(any(limit == 10 for limit, _ in claims), "the side never folded")
        self.assertIsNone(r.inbox.get(stop.inbox_id)["outcome"])
        self.assertEqual(self.clients[0].interrupts, 0)
        self.assertFalse(any(INTERRUPT in sources for _, sources in claims), claims)
        held["on"] = False
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "done", 20))
        row = r.inbox.get(stop.inbox_id)
        self.assertEqual((row["outcome"], row["claimant"]), ("delivered", primary.session_id))
        self.assertTrue(_wait(lambda: r.inbox.get(op.inbox_id)["state"] == "done", 20))


class TestGenerationOnFile(SideCase):
    def test_a_fallback_to_the_old_session_records_the_old_generation(self):
        """Review M3: the file says which generation the session belongs to,
        so a restart still resets a session the fallback kept."""
        r = self.side([[_turn("s-1")], [], [_turn("s-1")]], fail=(1,))
        r.start()
        a = r.enqueue(_peer("one"))
        self.assertTrue(self.done(r, a))
        boot.bump_generation(self.home)
        b = r.enqueue(_peer("two"))
        self.assertTrue(self.done(r, b))
        self.assertEqual(asked_resume(self.clients[2].options), "s-1")
        r.stop(timeout=5)
        saved = json.loads((self.home / "data" / "runner-session-peer.json").read_text())
        self.assertEqual((saved["session_id"], saved["generation"]), ("s-1", 0))

    def test_bump_generation_never_writes_the_file_in_place(self):
        """Review M2: a side boundary reading mid-bump must not see an empty
        file (read as generation 0)."""
        real = pathlib.Path.write_text

        def guarded(path, *a, **k):
            assert path.name != "generation.txt", "generation.txt written in place"
            return real(path, *a, **k)

        with mock.patch.object(pathlib.Path, "write_text", guarded):
            self.assertEqual(boot.bump_generation(self.home), 1)
        self.assertEqual(boot.read_generation(self.home), 1)


if __name__ == "__main__":
    unittest.main()
