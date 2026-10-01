"""The tmux kind's rules beyond the contract suite: what closes a row and
when, what a turn start nobody typed is, what a restart does with rows
already typed, and the screens the runner never types into."""
import json
import threading
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.tmux_pane import NO_PANE, Outcome
from cousin_lib.runner.tmux_runner import TmuxRunner
from tests._hermetic import HermeticCase
from tests.runner._fake_pane import FakePane
from tests.runner._home import temp_home


WAIT_S = 30.0   # a deadline, not a delay: _wait returns as soon as the condition holds


def _wait(pred, timeout=WAIT_S):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _result_append_fails_once(r):
    """The runner's first `result` append raises (a full disk, say): the
    rows it names must still close as its branch closes them."""
    real, said = r.stream.append, []

    def append(kind, payload):
        if kind == "result" and not said:
            said.append(payload)
            raise OSError("No space left on device")
        return real(kind, payload)
    r.stream.append = append
    return said


class Case(HermeticCase):
    def runner(self, pane=None, **kw):
        self.home = getattr(self, "home", None) or temp_home(self)
        panes = []

        def factory(path):
            opts = dict({"context_home": self.home}, **kw)
            p = pane(path) if pane else FakePane(path, on_prompt=getattr(self, "on_prompt", None), **opts)
            panes.append(p)
            return p
        deadline = kw.pop("handoff_deadline_s", None)
        r = TmuxRunner(self.home, account=None, pane_factory=factory, config_dir=self.home / ".cfg",
                       handoff_deadline_s=deadline,
                       launch_argv=lambda sid, fresh: ["claude", sid] + (["--fresh"] if fresh else []))
        # Cleanups run last-in first-out: the runner stops, then the fake
        # CLIs end their turns, then the home goes.
        self.addCleanup(FakePane.quiesce_all)
        self.addCleanup(lambda: r.stop(timeout=5))
        self.panes = panes
        return r

    def outcome(self, r, rec):
        row = r.inbox.get(rec.inbox_id)
        return (row["state"], row["outcome"], row["detail"])

    def kinds(self, r):
        return [e["kind"] for e in r.events()]

    def write(self, r, obj):
        with r._path.open("a") as fh:
            fh.write(json.dumps(obj) + "\n")

    def hook_record(self, r, pane):
        """What the pane's SessionStart hook writes (tmux_hook): the
        record a later runner needs before it adopts `pane`."""
        (self.home / "run" / "tmux-session.json").write_text(json.dumps(
            {"session_id": r.session_id(), "transcript_path": str(r._path),
             "source": "startup", "pid": pane.pid()}))


class TestTyping(Case):
    def test_a_blocked_row_is_requeued_never_failed(self):
        r = self.runner(attention="trust")
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: r.login_required()))
        # the flag is set a moment before the file is written
        self.assertTrue(_wait(lambda: (self.home / "data" / "login-required.json").exists()))
        self.assertEqual(self.outcome(r, rec)[0], "queued")
        self.assertEqual(self.panes[0].typed, [])

    def test_a_failed_paste_closes_the_row_failed(self):
        class Dead(FakePane):
            def type_row(self, first_line, body):
                return Outcome.FAILED
        r = self.runner(pane=lambda path: Dead(path))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "failed"))

    def test_the_first_line_carries_the_nonce_and_the_sender(self):
        r = self.runner()
        r.start()
        r.enqueue(Item("peer:kestrel", "chat", "the body", sender="Kestrel"))
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].typed))
        first, body = self.panes[0].typed[0]
        self.assertRegex(first, r"^\[inbox:[0-9a-f]{12}\] \[peer:kestrel\] chat from Kestrel$")
        self.assertEqual(body, "the body")

    def test_a_sender_with_controls_is_one_line_never_a_failed_row(self):
        r = self.runner()
        r.start()
        rec = r.enqueue(Item("peer:kestrel", "chat", "the body", sender="Kes\ntrel\x1b[201~\r"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        first = self.panes[0].typed[0][0]
        self.assertNotRegex(first, "[\x00-\x1f\x7f]")

    def test_the_rewind_selector_gets_one_escape_and_nothing_typed(self):
        r = self.runner(attention="rewind")
        r.start()
        r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: "Escape" in self.panes[0].keys))
        self.assertEqual(self.panes[0].typed, [])


class TestStoppingClaimsNothing(Case):
    """A runner asked to stop (held or not)
    claims nothing new; it only finishes or settles what it has."""

    def test_a_runner_asked_to_stop_types_no_queued_row(self):
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: r.state() == "idle" and self.panes))
        r.begin_stop()
        rec = r.enqueue(Item("operator:wren", "chat", "queued at the stop", sender="Wren"))
        time.sleep(0.5)
        self.assertEqual(self.outcome(r, rec)[0], "queued")
        self.assertEqual(self.panes[0].typed, [])

    def test_a_claim_that_races_the_stop_goes_back_untyped(self):
        r = self.runner()
        real = r.inbox.claim

        def claim(**kw):
            rows = real(**kw)
            if rows:
                r.begin_stop()                 # the stop lands while the claim runs
            return rows
        r.inbox.claim = claim
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "raced", sender="Wren"))
        self.assertTrue(_wait(lambda: r._stopping.is_set()))
        # the requeue follows the stop on the runner's thread: waited for,
        # never a fixed sleep ('claimed' can last past 0.3 s on a loaded host)
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[0] == "queued"), self.outcome(r, rec))
        self.assertEqual(self.panes[0].typed, [])


class TestTurns(Case):
    def test_a_turn_start_nobody_typed_is_a_foreign_turn(self):
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: r._path is not None and r._path.exists()))
        self.write(r, {"type": "user", "promptSource": "typed", "promptId": "px",
                       "message": {"role": "user", "content": "someone at the keyboard"}})
        self.assertTrue(_wait(lambda: "foreign_turn" in self.kinds(r)))
        # the event is appended a moment before the state moves
        self.assertTrue(_wait(lambda: r.state() == "running"), r.state())
        self.write(r, {"type": "system", "subtype": "turn_duration", "durationMs": 1})
        self.assertTrue(_wait(lambda: r.state() == "idle"))

    def test_a_new_turn_start_ends_the_live_turn_as_interrupted(self):
        r = self.runner(slow=True)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "slow", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.write(r, {"type": "user", "promptSource": "queued", "promptId": "pq",
                       "message": {"role": "user", "content": "sent now from the pane"}})
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        results = lambda: [e["payload"] for e in r.events() if e["kind"] == "result"]
        self.assertTrue(_wait(results), "the row closes a moment before its result event")
        self.assertTrue(results()[0]["interrupted"])

    def test_an_interrupt_row_whose_escape_was_refused_closes_failed(self):
        """A refused Escape (an attention screen showing) is
        not a delivered interrupt; the turn runs on."""
        from cousin_lib.runner.base import INTERRUPT
        r = self.runner(slow=True, slow_s=3.0)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "slow", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.panes[0]._attention = "mcp_approval"      # a dialog came up mid-turn
        stop = r.enqueue(Item("system", INTERRUPT, "interrupt asked from the console",
                              sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, stop)[0] == "done"))
        self.assertEqual(self.outcome(r, stop)[1], "failed")
        self.assertIn("refused", self.outcome(r, stop)[2])
        self.assertNotIn("Escape", self.panes[0].keys)
        self.panes[0]._attention = None
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))

    def test_a_closed_nonce_seen_again_is_a_duplicate(self):
        r = self.runner()
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "once", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        nonce = self.panes[0].typed[0][0][len("[inbox:"):len("[inbox:") + 12]
        self.write(r, {"type": "user", "promptSource": "typed", "promptId": "pd",
                       "message": {"role": "user", "content": "[inbox:%s] again" % nonce}})
        self.assertTrue(_wait(lambda: "duplicate_delivery" in self.kinds(r)))


class TestCursor(Case):
    def test_the_cursor_is_saved_per_line_so_a_failure_never_replays_the_ones_before(self):
        """_pump once saved the cursor after ALL entries, so one
        that raised replayed every entry before it every 0.2 s."""
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: r._path is not None and r._path.exists()))
        handled, real = [], r._handle

        def handle(e):
            handled.append(e.prompt_id)
            if e.prompt_id == "p2":
                raise RuntimeError("scripted")
            real(e)
        r._handle = handle
        for pid in ("p1", "p2"):
            self.write(r, {"type": "user", "promptSource": "typed", "promptId": pid,
                           "message": {"role": "user", "content": "someone %s" % pid}})
        self.assertTrue(_wait(lambda: handled.count("p2") >= 3))
        self.assertEqual(handled.count("p1"), 1, "the line before the failing one is not replayed")
        # after three replays (four failures) the line is skipped, said once
        offset = len(r._path.read_text().splitlines()[0]) + 1
        skipped = lambda: [e["payload"]["error"] for e in r.events() if e["kind"] == "error"
                           and "skipped" in e["payload"]["error"]]
        self.assertTrue(_wait(skipped))
        self.assertIn("offset %d" % offset, skipped()[0])
        self.write(r, {"type": "user", "promptSource": "typed", "promptId": "p3",
                       "message": {"role": "user", "content": "someone p3"}})
        self.assertTrue(_wait(lambda: "p3" in handled))
        self.assertEqual(handled.count("p2"), 4)
        self.assertEqual(len(skipped()), 1)


class TestSkippedLines(Case):
    """A line whose handling keeps raising is skipped; if it ended the
    live turn, the turn is closed with the skip as the reason."""

    def test_a_skipped_turn_end_closes_the_live_turn(self):
        r = self.runner()
        orig = r.inbox.done
        fails = {"n": 0}

        def flaky_done(inbox_id, outcome, detail=""):
            if detail.startswith("turn ") and fails["n"] < 4:
                fails["n"] += 1
                raise OSError("transient: disk full")
            return orig(inbox_id, outcome, detail)
        r.inbox.done = flaky_done
        r.start()
        a = r.enqueue(Item("operator:wren", "chat", "first", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, a)[0] == "done"))
        self.assertEqual(self.outcome(r, a)[1], "delivered")
        self.assertIn("skipped", self.outcome(r, a)[2])
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertIsNone(r._live)
        b = r.enqueue(Item("operator:wren", "chat", "second", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, b)[1] == "delivered"))
        self.assertEqual(len(self.panes[0].typed), 2)

    def test_a_skipped_api_error_fails_the_rows(self):
        r = self.runner(fail_first=True)
        real = r._fail_live
        calls = {"n": 0}

        def failing(message):
            calls["n"] += 1
            raise RuntimeError("scripted")
        r._fail_live = failing
        r.start()
        a = r.enqueue(Item("operator:wren", "chat", "first", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, a)[0] == "done"))
        self.assertEqual(self.outcome(r, a)[1], "failed")
        self.assertIn("skipped", self.outcome(r, a)[2])
        self.assertEqual(calls["n"], 4)
        self.assertTrue(_wait(lambda: r.state() == "idle"))

    def test_a_skipped_limit_requeues_the_rows_and_waits_as_a_limit_does(self):
        """A limit end, even when its line is skipped: the taken row is
        requeued, never failed, and the runner is rate_limited."""
        self.on_prompt = lambda pane, first, body: "limit"
        r = self.runner()
        real, calls = r._limit_live, {"n": 0}

        def limit_live():
            calls["n"] += 1
            if calls["n"] <= 4:
                raise RuntimeError("scripted")
            real()
        r._limit_live = limit_live
        r.start()
        a = r.enqueue(Item("operator:wren", "chat", "over the limit", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "rate_limited"))
        self.assertEqual(self.outcome(r, a)[0], "queued", "requeued, never failed")
        self.assertIsNone(r._live)
        self.assertTrue(any("skipped" in e["payload"]["error"] for e in r.events()
                            if e["kind"] == "error"))

    def test_the_entries_before_a_failing_one_on_a_torn_line_are_handled_once(self):
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: r._path is not None and r._path.exists()))
        seen, real = [], r._handle

        def handle(e):
            seen.append(e.kind)
            if e.kind == "turn_start" and e.prompt_id == "p1":
                raise RuntimeError("scripted")
            real(e)
        r._handle = handle
        start = json.dumps({"type": "user", "promptSource": "typed", "promptId": "p1",
                            "message": {"role": "user", "content": "someone p1"}})
        with r._path.open("a") as fh:
            fh.write('{"type": "assistant", "mess' + start + "\n")
        self.assertTrue(_wait(lambda: seen.count("turn_start") >= 4))
        time.sleep(0.5)
        self.assertEqual(seen.count("other"), 1, "the torn fragment is handled once")
        self.assertEqual(seen.count("turn_start"), 4, "replayed three times, then skipped")


class TestCutPrefix(Case):
    """The expired notice's prefix stays until its row is
    TAKEN (a row retyped after it was not taken still carries it), and a
    rollover drops it (a new session had no turn cut)."""

    def test_the_prefix_stays_until_its_row_is_taken(self):
        class Deaf(FakePane):
            def _play(self, first_line, body, n):
                with self._lock:
                    self._busy = False
        r = self.runner(pane=lambda path: Deaf(path, context_home=self.home))
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        r._cut_prefix = {"text": "[runner] the previous turn was cut short", "clears_note": False}
        rec = r.enqueue(Item("operator:wren", "chat", "carries it", sender="Wren"))
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].typed))
        first, body = self.panes[0].typed[0]
        self.assertTrue(body.startswith("[runner] the previous turn was cut short"))
        time.sleep(0.3)
        self.assertIsNotNone(r._cut_prefix, "typed is not taken")
        self.write(r, {"type": "user", "promptSource": "typed", "promptId": "pt",
                       "message": {"role": "user", "content": first}})
        self.assertTrue(_wait(lambda: r._cut_prefix is None))
        self.write(r, {"type": "system", "subtype": "turn_duration", "durationMs": 1})
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))

    def test_a_rollover_drops_the_prefix(self):
        self.home = temp_home(self)
        (self.home / ".cfg").mkdir()
        self.on_prompt = _handoff_tool
        r = self.runner(handoff_deadline_s=5)
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        r._cut_prefix = {"text": "[runner] the previous turn was cut short", "clears_note": False}
        out = r.rollover("contract")
        self.assertTrue(out["ok"], out)
        self.assertIsNone(r._cut_prefix)
        self.assertTrue(_wait(lambda: self.panes and self.panes[-1].typed))    # the new session's digest
        self.assertFalse(any(b.startswith("[runner] the previous turn") for p in self.panes
                             for _f, b in p.typed))


class TestGiveUpHold(Case):
    """A give-up survives the supervisor: exit 2 (never
    restarted), and a marker the next start honours for an hour, or until
    an explicit start clears it."""

    def marker(self):
        return self.home / "data" / "run" / "tmux-giving-up.json"

    def test_a_give_up_exits_2_and_the_next_start_honours_it(self):
        from cousin_lib.runner import main as runner_main
        r = self.runner(pane=lambda path: DiesAtBoot(path, boot_s=1.0, context_home=self.home))
        r.reopen_base_s = 0.05
        self.assertEqual(runner_main._serve(r, False), 2)
        data = json.loads(self.marker().read_text())
        self.assertIn("5 starts in a row", data["reason"])
        self.assertLess(time.time() - data["at"], 60)
        started = sum(len(p.started) for p in self.panes)
        r2 = self.runner(pane=lambda path: DiesAtBoot(path, boot_s=1.0, context_home=self.home))
        t = time.monotonic()
        self.assertEqual(runner_main._serve(r2, False), 2)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(self.panes, [], "no pane started under a give-up")
        self.assertIn("cousin-supervisor start", r2.fatal)
        self.assertEqual(r2.state(), "stopped")
        self.assertGreater(started, 0)

    def test_a_give_up_older_than_an_hour_is_dropped(self):
        self.home = temp_home(self)
        (self.home / "data" / "run").mkdir(parents=True)
        self.marker().write_text(json.dumps({"reason": "old", "at": time.time() - 3700}))
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertFalse(self.marker().exists())


class TestContext(Case):
    """The block the launcher appends on a fresh start,
    the pointer a resumed session gets, the live turn for the stdio server."""

    def context(self):
        return self.home / "data" / "run" / "tmux-context.md"

    def pointer(self):
        return self.home / "data" / "run" / "tmux-resume.md"

    def test_a_fresh_start_writes_the_block_before_the_pane_starts(self):
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].started))
        self.assertTrue(_wait(lambda: self.panes[0].alive()))
        self.assertEqual(self.panes[0].launch_refused, 0)
        self.assertIn("You run on an interactive Claude Code pane", self.context().read_text())
        self.assertNotIn("Framework law", self.pointer().read_text() if self.pointer().exists() else "")

    def test_the_block_is_private_to_the_cousins_user(self):
        """#154: the block holds the law and the contract; the launcher hands
        its path to the CLI, so the file is the only copy: 0600 in a 0700 dir."""
        self.home = temp_home(self)
        run = self.home / "data" / "run"
        run.mkdir(parents=True, exist_ok=True)
        run.chmod(0o755)
        self.context().write_text("old")
        self.context().chmod(0o644)
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertIn("You run on an interactive Claude Code pane", self.context().read_text())
        self.assertEqual(self.context().stat().st_mode & 0o777, 0o600)
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)

    def test_a_fresh_start_records_the_generation_start_and_a_resume_keeps_it(self):
        from cousin_lib import boot
        before = time.time()
        r = self.runner()
        r.start()
        # recorded just after the pane starts: waited for, never raced
        self.assertTrue(_wait(lambda: (boot.generation_started(self.home) or 0) >= before))
        rec = r.enqueue(Item("operator:wren", "chat", "one turn", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        r._stop.set()
        r._thread.join(3)
        self.panes[0].die()
        boot.mark_generation_start(self.home, now=1000.0)
        r2 = self.runner()
        r2.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertNotIn("--fresh", self.panes[0].started[0][0])     # resumed
        self.assertEqual(boot.generation_started(self.home), 1000.0)

    def test_a_resume_gets_a_pointer_to_the_block_saying_what_changed(self):
        sid = "5e55a000-0000-4000-8000-000000000002"
        self.home = temp_home(self)
        (self.home / "data" / "runner-session.json").write_text(json.dumps(
            {"session_id": sid, "kind": "sdk"}))                 # born on the other kind
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertNotIn("--fresh", self.panes[0].started[0][0])
        self.assertTrue(self.context().exists())
        text = self.pointer().read_text()
        self.assertIn(str(self.context()), text)
        self.assertIn("did not start with it", text)
        r.stop(timeout=5)
        r2 = self.runner()
        r2.start()                                                # still not born with it
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertIn("did not start with it", self.pointer().read_text())

    def test_a_session_born_with_the_block_is_told_it_is_unchanged(self):
        r = self.runner()
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "one turn", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        r._stop.set()
        r._thread.join(3)
        self.panes[0].die()
        r2 = self.runner()
        r2.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertIn("unchanged since this session started", self.pointer().read_text())

    def test_the_live_turn_is_written_for_the_stdio_server_and_cleared_at_its_end(self):
        from cousin_lib.runner import tmux_turn
        r = self.runner(slow=True)
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.hook_record(r, self.panes[0])
        rec = r.enqueue(Item("operator:wren", "chat", "slow", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertEqual(tmux_turn.live(self.home), (True, ("operator:wren",)))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        self.assertTrue(_wait(lambda: tmux_turn.live(self.home) == (False, ())))
        self.assertFalse((self.home / "run" / "turn.json").exists())


class TestPaneLoss(Case):
    """The CLI exits under a live runner. The runner stops
    claiming, says so, settles the rows (never `failed` for a dead
    pane) and resumes the session in a new pane, with a backoff."""

    def lost(self, r):
        return [e["payload"] for e in r.events()
                if e["kind"] == "system" and e["payload"].get("subtype") == "pane_lost"]

    def test_rows_queued_on_a_dead_pane_wait_for_the_resumed_pane(self):
        r = self.runner()
        r.reopen_base_s = 0.1
        r.start()
        first = r.enqueue(Item("operator:wren", "chat", "before", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, first)[1] == "delivered"))
        sid = r.session_id()
        self.panes[0].die()                        # the CLI exits under a live runner
        recs = [r.enqueue(Item("operator:wren", "chat", "m%d" % i, sender="Wren")) for i in range(5)]
        self.assertTrue(_wait(lambda: all(self.outcome(r, x)[1] == "delivered" for x in recs)),
                        [self.outcome(r, x) for x in recs])
        self.assertEqual(len(self.lost(r)), 1)
        self.assertEqual(len(self.panes), 2)
        self.assertEqual(self.panes[1].started[0][0], ["claude", sid], "resumed, not fresh")
        self.assertEqual(r.session_id(), sid)

    def test_a_turn_cut_by_the_pane_dying_closes_delivered_and_the_model_is_told(self):
        r = self.runner(slow=True, slow_s=5.0)
        r.reopen_base_s = 0.1
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.panes[0].die()
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        self.assertIn("pane", self.outcome(r, rec)[2])
        self.assertTrue(_wait(lambda: len(self.panes) == 2 and any(
            "cut short" in body for _f, body in self.panes[1].typed)))
        self.assertEqual(self.lost(r)[0]["cut"], [rec.inbox_id])
        self.assertEqual(self.panes[1].typed and len([t for t in self.panes[1].typed
                                                     if "mid-turn" in t[1]]), 0, "never retyped")

    def test_a_pane_that_will_not_start_is_retried_with_a_backoff(self):
        tries = []

        class Stillborn(FakePane):
            def start(self, argv, *, cwd, env_base):
                tries.append(time.monotonic())
                super().start(argv, cwd=cwd, env_base=env_base)
                if len(tries) in (2, 3):
                    self._alive = False             # the CLI exits at once
        r = self.runner(pane=lambda path: Stillborn(path, context_home=self.home))
        r.reopen_base_s = 0.3
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.panes[0].die()
        rec = r.enqueue(Item("operator:wren", "chat", "after", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        self.assertEqual(len(tries), 4)
        gaps = [b - a for a, b in zip(tries[1:], tries[2:])]
        self.assertGreater(gaps[1] - gaps[0], 0.15, "the wait doubles: %s" % gaps)
        self.assertEqual(len(self.lost(r)), 1, "one event per loss, not per try")

    def test_a_blocked_row_backs_off_and_is_said_once(self):
        class Full(FakePane):
            def type_row(self, first_line, body):
                self.type_calls += 1
                return Outcome.BLOCKED             # text in the box nobody clears
        r = self.runner(pane=lambda path: Full(path, context_home=self.home))
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        rec = r.enqueue(Item("operator:wren", "chat", "waits", sender="Wren"))
        time.sleep(2.0)
        self.assertEqual(self.outcome(r, rec)[0], "queued")
        self.assertLess(self.panes[0].type_calls, 8, "not a 10 Hz spin")
        blocked = [e for e in r.events() if e["kind"] == "system"
                   and e["payload"].get("subtype") == "typing_blocked"]
        self.assertEqual(len(blocked), 1)


class DiesAtBoot(FakePane):
    """A CLI that exits 0.4 s after every start, before it draws its box (a
    resume the CLI refuses at boot); `why` is what its launcher left."""
    why = None

    def start(self, argv, *, cwd, env_base):
        super().start(argv, cwd=cwd, env_base=env_base)
        if self.why and self.context_home is not None:
            (self.context_home / "data" / "run" / "tmux-launch-exit.txt").write_text(self.why)
        if self._alive:
            threading.Timer(0.4, self.die).start()


class TestBootLoop(Case):
    """A pane that dies at boot is not restarted every
    second forever; after five failed starts the runner gives up, errored,
    naming why, and its worker ends (exit 3: the supervisor sees it)."""

    def starts(self):
        return sum(len(p.started) for p in self.panes)

    def failing(self, r):
        return [e["payload"] for e in r.events() if e["kind"] == "system"
                and e["payload"].get("subtype") == "pane_failing"]

    def test_a_pane_that_dies_at_boot_is_given_up_on(self):
        r = self.runner(pane=lambda path: DiesAtBoot(path, boot_s=1.0, context_home=self.home))
        r.reopen_base_s = 0.1
        r.start()
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        self.assertEqual(self.starts(), 5)
        self.assertEqual(r.state(), "errored")
        self.assertEqual(len(self.failing(r)), 1)
        self.assertIn("5", r.fatal)
        time.sleep(1.0)
        self.assertEqual(self.starts(), 5, "nothing started after giving up")

    def test_the_launchers_refusal_is_named(self):
        class Refused(DiesAtBoot):
            why = "tmux-launch: refused, a claude-token account (refused)"
        r = self.runner(pane=lambda path: Refused(path, boot_s=1.0, context_home=self.home))
        r.reopen_base_s = 0.05
        r.start()
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        self.assertIn("claude-token account (refused)", self.failing(r)[0]["reason"])
        self.assertIn("claude-token account (refused)", r.fatal)

    def test_the_backoff_grows_across_boot_deaths(self):
        r = self.runner(pane=lambda path: DiesAtBoot(path, boot_s=1.0, context_home=self.home))
        r.reopen_base_s = 1.0
        r.start()
        time.sleep(5.0)
        self.assertLessEqual(self.starts(), 4, "was 19 in 10 s on the round-1 code")

    def test_a_pane_that_stays_up_is_proven_and_the_count_starts_over(self):
        lives = []

        class Proves(DiesAtBoot):
            def start(self, argv, *, cwd, env_base):
                lives.append(1)
                if len(lives) == 3:                        # this one boots and stays
                    return FakePane.start(self, argv, cwd=cwd, env_base=env_base)
                return super().start(argv, cwd=cwd, env_base=env_base)
        r = self.runner(pane=lambda path: Proves(path, boot_s=1.0, context_home=self.home))
        r.reopen_base_s = 0.05
        r.probation_s = 1.5
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "after the boot loop", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        self.assertTrue(r.worker_alive())
        self.assertTrue(_wait(lambda: r._reopen_fails == 0))

    def test_a_pane_that_passes_its_proof_and_dies_again_and_again_is_given_up_on(self):
        """A CLI that outlives the proof and then dies, over and
        over, is capped by a sliding window: more than 5 losses in 10 min."""
        class DiesLater(FakePane):
            def start(self, argv, *, cwd, env_base):
                super().start(argv, cwd=cwd, env_base=env_base)
                if self._alive:
                    threading.Timer(0.6, self.die).start()
        r = self.runner(pane=lambda path: DiesLater(path, context_home=self.home))
        r.probation_s = 0.3
        r.start()
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        self.assertEqual(self.starts(), 6)
        self.assertEqual(r._reopen_fails, 0, "each pane had proven itself")
        self.assertEqual(r.state(), "errored")
        failing = self.failing(r)
        self.assertEqual(len(failing), 1)
        self.assertIn("6 pane losses within 600 s", failing[0]["reason"])
        self.assertEqual((failing[0]["losses"], failing[0]["window_s"]), (6, 600.0))
        self.assertNotIn("tries", failing[0], "not a count of failed starts (minor 6)")
        self.assertIn("6 pane losses", r.fatal)

    def test_a_pane_that_draws_its_box_and_dies_at_once_is_still_a_failed_start(self):
        """A pane that dies at boot with its box already drawn."""
        r = self.runner(pane=lambda path: DiesAtBoot(path, context_home=self.home))
        r.reopen_base_s = 0.1
        r.start()
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        self.assertEqual(self.starts(), 5)


class TestUnreachable(Case):
    """One failed has-session is not a dead pane."""

    def test_one_failed_has_session_mid_turn_settles_nothing(self):
        class Flaky(FakePane):
            lie = False

            def alive(self):
                if self.lie:
                    self.lie = False
                    return False
                return super().alive()
        r = self.runner(pane=lambda path: Flaky(path, slow=True, slow_s=2.0, context_home=self.home))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "slow", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.panes[0].lie = True
        r._next_alive = 0.0
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        self.assertIn("turn", self.outcome(r, rec)[2])
        subs = [e["payload"].get("subtype") for e in r.events() if e["kind"] == "system"]
        self.assertNotIn("pane_lost", subs)
        self.assertEqual(len(self.panes), 1)

    def test_tmux_silent_while_the_cli_runs_settles_nothing(self):
        class Deaf(FakePane):
            deaf = False

            def alive(self):
                return False if self.deaf else super().alive()

            def process_alive(self, pid):
                return True if self.deaf else super().process_alive(pid)
        r = self.runner(pane=lambda path: Deaf(path, slow=True, slow_s=2.5, context_home=self.home))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "slow", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.panes[0].deaf = True
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        self.assertIn("turn", self.outcome(r, rec)[2])
        subs = [e["payload"].get("subtype") for e in r.events() if e["kind"] == "system"]
        self.assertNotIn("pane_lost", subs)
        self.assertEqual(subs.count("pane_unreachable"), 1)

    def test_a_reopen_that_finds_the_pane_alive_recovers_its_live_turn(self):
        shared = []

        class Liar(FakePane):
            lies = 0

            def alive(self):
                if self.lies:
                    self.lies -= 1
                    return False
                return super().alive()

            def process_alive(self, pid):
                if self.lies_proc:
                    self.lies_proc -= 1
                    return False
                return super().process_alive(pid)
            lies_proc = 0

        def one_pane(path):
            if not shared:
                shared.append(Liar(path, slow=True, slow_s=2.5, context_home=self.home))
            return shared[0]
        r = self.runner(pane=one_pane)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "slow", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.hook_record(r, shared[0])
        shared[0].lies, shared[0].lies_proc = 2, 1          # read as lost, then adopted
        r._next_alive = 0.0
        self.assertTrue(_wait(lambda: any(e["kind"] == "system" and e["payload"].get("subtype")
                                          == "pane_reopened" for e in r.events())))
        reopened = [e["payload"] for e in r.events() if e["kind"] == "system"
                    and e["payload"].get("subtype") == "pane_reopened"][0]
        self.assertEqual(reopened["source"], "adopted")
        self.assertEqual(r.state(), "running", "the live turn is known again")
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        time.sleep(0.5)
        self.assertEqual(len(shared[0].typed), 1, "nothing typed into the live turn")

    def test_an_unreachable_pane_is_asked_less_often_and_lost_after_a_bound(self):
        """tmux stops answering for good
        while the CLI's pid runs on. The check backs off to 5 s, nothing is
        typed meanwhile, and after the bound the pane is lost and reopened."""
        calls = {"n": 0}

        class Gone(FakePane):
            cut = False

            def alive(self):
                if self.cut:
                    calls["n"] += 1
                    return False
                return super().alive()

            def attention(self):
                if self.cut:
                    calls["n"] += 1
                    return NO_PANE
                return super().attention()

            def process_alive(self, pid):
                if self.cut:
                    return not self.sigkills          # the CLI runs until it is SIGKILLed
                return super().process_alive(pid)
        r = self.runner(pane=lambda path: Gone(path, context_home=self.home))
        r.kill_grace_s, r.kill_bound_s = 0.1, 1.0
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        time.sleep(0.3)
        self.panes[0].cut = True
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        time.sleep(5.0)
        self.assertLess(calls["n"], 15, "was 150 tmux calls in 5 s")
        self.assertEqual(self.outcome(r, rec)[0], "queued")
        r.unreachable_lost_s = 0.0
        self.assertTrue(_wait(lambda: len(self.panes) == 2 and self.panes[1].alive()),
                        [(e["kind"], e["payload"]) for e in r.events() if e["kind"] in ("error", "system")])
        self.assertTrue(self.panes[0].sigkills, "the old CLI is killed before the new pane")
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))

    def test_the_old_cli_is_gone_before_a_reopen(self):
        class Lingers(FakePane):
            """Gone when the loss is checked; seen again at the reopen and
            outliving the first SIGKILL, gone after the second. Driven by
            the kills, never by a count of polls: the polls one kill bound
            holds depend on the host's load, and a count ran the runner into
            its give-up on a loaded host."""
            lingering = None

            def process_alive(self, pid):
                if self.lingering is None:
                    return super().process_alive(pid)
                if not self.lingering:
                    self.lingering = True        # the loss check: gone
                    return False
                return len(self.sigkills) < 2
        r = self.runner(pane=lambda path: Lingers(path, context_home=self.home))
        r.kill_grace_s, r.kill_bound_s = 0.1, 0.3
        r.reopen_base_s = 0.05
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.panes[0].lingering = False
        self.panes[0].die()                  # lost, then the old pid is seen again at the reopen
        self.assertTrue(_wait(lambda: len(self.panes) >= 2 and self.panes[-1].alive()))
        self.assertTrue(self.panes[0].sigkills, "the old CLI was killed before the new pane")
        errors = [e["payload"]["error"] for e in r.events() if e["kind"] == "error"]
        self.assertTrue(any("still running" in e for e in errors), errors)

    def test_a_poll_late_past_the_bound_still_sends_the_sigkill(self):
        """A loaded host can hold one poll past both the grace and the
        bound: the CLI is SIGKILLed before the runner gives up on it, never
        left running unkilled."""
        class Late(FakePane):
            polls = 0

            def process_alive(self, pid):
                self.polls += 1
                if self.polls == 1:
                    time.sleep(0.5)              # the first poll comes back late
                return not self.sigkills         # runs until it is SIGKILLed
        r = self.runner()
        r.kill_grace_s, r.kill_bound_s = 0.1, 0.3
        r.pane = Late(self.home / "transcript.jsonl", context_home=self.home)
        self.assertTrue(r._gone(4242))
        self.assertEqual(r.pane.sigkills, [4242])


class TestFreshStartAndNotices(Case):
    def test_a_fresh_start_removes_a_stale_resume_pointer(self):
        self.home = temp_home(self)
        (self.home / "data" / "run").mkdir(parents=True)
        (self.home / "data" / "run" / "tmux-resume.md").write_text("an old session's pointer")
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        self.assertFalse((self.home / "data" / "run" / "tmux-resume.md").exists())

    def test_the_notice_clock_waits_out_a_login_screen(self):
        r = self.runner(slow=True, slow_s=5.0)
        r.reopen_base_s = 0.1
        r.notice_wait_s = 0.8
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.panes[0].die()
        self.assertTrue(_wait(lambda: len(self.panes) == 2 and self.panes[1].alive()))
        self.panes[1]._attention = "login"
        time.sleep(1.5)                       # past the notice's bound, all of it a login wait
        self.panes[1]._attention = None
        self.assertTrue(_wait(lambda: any("cut short" in b for _f, b in self.panes[1].typed)))
        self.assertEqual(self.outcome(r, rec)[1], "delivered")

    def test_a_notice_that_expires_untyped_rides_on_the_next_row(self):
        r = self.runner(slow=True, slow_s=5.0)
        r.reopen_base_s = 0.1
        r.notice_wait_s = 0.3
        r.start()
        first = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        booting = []

        def slow_boot(path):
            p = FakePane(path, boot_s=1.5, context_home=self.home)
            booting.append(p)
            return p
        r._pane_factory = slow_boot
        self.panes[0].die()
        self.assertTrue(_wait(lambda: any(e["kind"] == "system" and e["payload"].get("subtype")
                                          == "notice_not_typed" for e in r.events())))
        rec = r.enqueue(Item("operator:wren", "chat", "the next row", sender="Wren"))
        self.assertTrue(_wait(lambda: booting and booting[-1].typed))
        body = booting[-1].typed[0][1]
        self.assertTrue(body.startswith("[runner] "), body)
        self.assertIn("cut short", body.splitlines()[0])
        self.assertIn("the next row", body)
        self.assertEqual(self.outcome(r, first)[1], "delivered")


class TestStop(Case):
    def test_an_unheld_stop_leaves_the_pane_and_a_held_one_kills_it(self):
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        r.stop(timeout=5)
        self.assertEqual(self.panes[0].kills, 0)
        self.home = self.home                         # same home, a second runner
        r2 = self.runner()
        (self.home / "run").mkdir(exist_ok=True)
        (self.home / "run" / "held").write_text("now by test")
        r2.start()
        self.assertTrue(_wait(lambda: len(self.panes) == 1 and self.panes[0].alive()))
        r2.stop(timeout=5)
        self.assertEqual(self.panes[0].kills, 1)


    def test_a_stop_mid_turn_waits_for_the_turns_end_and_closes_the_row_once(self):
        r = self.runner(slow=True, slow_s=5.0)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        (self.home / "run" / "held").write_text("2026-09-24T23:00:00+00:00 cousin-migrate")
        r.stop(timeout=5)
        self.assertEqual(self.outcome(r, rec)[:2], ("done", "delivered"))
        self.assertIn("interrupted", self.outcome(r, rec)[2], "closed by the turn's own end")
        from cousin_lib.runner import restart_note
        self.assertIn("cousin-migrate", restart_note.read(self.home)["held"])

    def test_a_held_stop_whose_turn_never_ends_settles_the_row_cut(self):
        class Unstoppable(FakePane):
            def key(self, name):
                self.keys.append(name)             # the Escape is never answered

            def kill(self):
                self._dead = True                  # a killed CLI writes nothing more
                super().kill()
        # The turn would run 120 s: the stop returns long before it (its
        # own 2 s, plus whatever a loaded host adds; 5.1 s has been seen).
        r = self.runner(pane=lambda path: Unstoppable(path, slow=True, slow_s=120.0,
                                                      context_home=self.home))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        (self.home / "run" / "held").write_text("2026-09-24T23:00:00+00:00 console")
        t = time.monotonic()
        r.stop(timeout=2)
        self.assertLess(time.monotonic() - t, 60.0, "bounded by the stop, not the turn")
        self.assertEqual(self.outcome(r, rec), ("done", "delivered", "cut by a requested stop"))
        self.assertEqual(r.inbox.requeue_stale(older_than_s=0.0), 0, "nothing left claimed")
        self.assertEqual(json.loads((self.home / "data" / "tmux-claims.json").read_text())["claims"], [])


class TestPaneLostInAStop(Case):
    """Under a systemd stop the unit's cgroup
    kill takes the tmux server while the runner stops. A pane lost once a
    stop was asked for is the stop's cut, worded as a restart's is
    ("cut by restart"; "cut by a requested stop" when held), and the next
    start is told."""

    def lose_the_pane_in_a_stop(self):
        r = self.runner(slow=True, slow_s=10.0)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r.begin_stop()                                 # the SIGTERM
        self.panes[0].die()                            # and the cgroup kill took tmux
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[0] == "done"))
        r.stop(timeout=4)
        return r, rec

    def test_unheld_it_reads_cut_by_restart_and_the_next_start_is_told(self):
        from cousin_lib.runner import restart_note
        r, rec = self.lose_the_pane_in_a_stop()
        self.assertEqual(self.outcome(r, rec), ("done", "delivered", "cut by restart"))
        note = restart_note.read(self.home)
        self.assertIsNotNone(note, "the cut is marked for the next start")
        self.assertNotIn("held", note)

    def test_held_it_reads_cut_by_a_requested_stop(self):
        from cousin_lib.runner import restart_note
        self.home = temp_home(self)
        (self.home / "run").mkdir(exist_ok=True)
        (self.home / "run" / "held").write_text("2026-09-25T01:00:00+00:00 cousin-migrate")
        r, rec = self.lose_the_pane_in_a_stop()
        self.assertEqual(self.outcome(r, rec), ("done", "delivered", "cut by a requested stop"))
        self.assertIn("cousin-migrate", restart_note.read(self.home)["held"])

    def test_a_pane_lost_in_a_stop_is_no_loss_toward_a_give_up(self):
        r = self.runner(slow=True, slow_s=10.0)
        r.loss_max = 0                                  # one counted loss would give up
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r.begin_stop()
        self.panes[0].die()                             # on probation, too: no failed start
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[0] == "done"))
        r.stop(timeout=4)
        self.assertFalse(r._gave_up)
        self.assertEqual(r._reopen_fails, 0)
        self.assertEqual(len(r._losses), 0)
        self.assertFalse((self.home / "data" / "run" / "tmux-giving-up.json").exists())
        self.assertFalse(any(e["kind"] == "system" and e["payload"].get("subtype") == "pane_failing"
                             for e in r.events()))

    def test_a_pane_lost_with_no_stop_still_reads_pane_loss(self):
        r = self.runner(slow=True, slow_s=10.0)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.panes[0].die()
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[0] == "done"))
        self.assertEqual(self.outcome(r, rec)[2], "cut by pane loss")


class TestRecovery(Case):
    """A second runner on the same home settles what the first typed."""

    def first_runner_types(self, **kw):
        r = self.runner(**kw)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "typed before the restart", sender="Wren"))
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].typed))
        return r, rec

    def test_a_row_taken_and_ended_while_no_runner_watched_closes_delivered_once(self):
        r, rec = self.first_runner_types(slow=True)
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r._stop.set()                                  # the runner dies; the CLI finishes the turn
        r._thread.join(3)
        path = r._path
        self.assertTrue(_wait(lambda: "turn_duration" in path.read_text()))
        r2 = self.runner(slow=True)
        r2.start()
        self.assertTrue(_wait(lambda: self.outcome(r2, rec)[1] == "delivered"))
        self.assertEqual(len(self.panes[-1].typed), 0, "nothing retyped")

    def test_a_taken_row_cut_by_a_restart_is_delivered_and_the_model_is_told(self):
        r, rec = self.first_runner_types(slow=True)
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r._stop.set()
        r._thread.join(3)
        self.panes[0].die()                            # the pane died with the unit
        r2 = self.runner()
        r2.start()
        self.assertTrue(_wait(lambda: self.outcome(r2, rec)[2] == "cut by restart"))
        self.assertEqual(self.outcome(r2, rec)[1], "delivered")
        self.assertTrue(_wait(lambda: any("cut short by a restart" in body
                                          for _f, body in self.panes[-1].typed)))

    def test_the_cut_notice_waits_for_a_pane_still_booting(self):
        """The pane's box appears 1.5 s after start."""
        r, rec = self.first_runner_types(slow=True)
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r._stop.set()
        r._thread.join(3)
        self.panes[0].die()
        r2 = self.runner(pane=lambda path: FakePane(path, boot_s=1.5, context_home=self.home))
        r2.start()
        self.assertTrue(_wait(lambda: self.outcome(r2, rec)[2] == "cut by restart"))
        self.assertTrue(_wait(lambda: any("cut short" in b and "restart" in b
                                          for _f, b in self.panes[-1].typed)))

    def test_a_turn_cut_by_a_requested_stop_is_told_as_one(self):
        class Unstoppable(FakePane):
            def key(self, name):
                self.keys.append(name)             # the Escape is never answered

            def kill(self):
                self._dead = True                  # a killed CLI writes nothing more
                super().kill()
        r = self.runner(pane=lambda path: Unstoppable(path, slow=True, slow_s=10.0,
                                                      context_home=self.home))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "mid-turn", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        (self.home / "run" / "held").write_text("2026-09-24T23:00:00+00:00 console")
        r.stop(timeout=2)
        (self.home / "run" / "held").unlink()        # the supervisor's start releases it
        r2 = self.runner()
        r2.start()
        self.assertTrue(_wait(lambda: self.panes and any(
            "a requested stop" in b for _f, b in self.panes[0].typed)))
        self.assertFalse(any("restarted" in b for _f, b in self.panes[0].typed))
        self.assertEqual(self.outcome(r2, rec)[2], "cut by a requested stop")
        from cousin_lib.runner import restart_note
        self.assertTrue(_wait(lambda: restart_note.read(self.home) is None), "taken once typed")

    def test_a_notice_that_cannot_be_typed_in_time_is_said(self):
        r, rec = self.first_runner_types(slow=True)
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r._stop.set()
        r._thread.join(3)
        self.panes[0].die()
        r2 = self.runner(pane=lambda path: FakePane(path, boot_s=60, context_home=self.home))
        r2.notice_wait_s = 0.5
        r2.start()
        said = lambda: [e["payload"] for e in r2.events() if e["kind"] == "system"
                        and e["payload"].get("subtype") == "notice_not_typed"]
        self.assertTrue(_wait(lambda: said()))
        self.assertIn("cut short", said()[0]["text"])

    def test_an_untaken_row_is_requeued_and_typed_again(self):
        class Deaf(FakePane):
            def _play(self, first_line, body, n):     # typed, never taken
                pass
        r = self.runner(pane=lambda path: Deaf(path))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "lost in the box", sender="Wren"))
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].typed))
        r._stop.set()
        r._thread.join(3)
        self.panes[0].die()
        r2 = self.runner()
        r2.start()
        self.assertTrue(_wait(lambda: self.outcome(r2, rec)[1] == "delivered"))

    def test_a_torn_line_merged_with_the_rows_turn_start_takes_the_row_once(self):
        class Deaf(FakePane):
            def _play(self, first_line, body, n):     # the test writes the CLI's side
                pass
        r = self.runner(pane=lambda path: Deaf(path))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "torn", sender="Wren"))
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].typed))
        first = self.panes[0].typed[0][0]
        with r._path.open("a") as fh:
            fh.write('{"type": "assistant", "mess')          # torn by a SIGKILL
            fh.write(json.dumps({"type": "user", "promptSource": "typed", "promptId": "p1",
                                 "message": {"role": "user", "content": first}}) + "\n")
            fh.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        time.sleep(0.3)
        self.assertEqual(len(self.panes[0].typed), 1, "never typed twice")

    def test_an_adopted_row_still_queued_in_the_cli_keeps_its_nonce(self):
        """A row typed and queued by the CLI behind a turn when the
        runner restarted is put back with its nonce, never retyped: its turn
        start takes it, once."""
        class Queuing(FakePane):
            holding = False

            def _play(self, first_line, body, n):     # queued behind a running turn
                self.holding = True
                with self._lock:
                    self._busy = False

            def queued(self):
                return self.holding

            def type_row(self, first_line, body):
                if self.holding:
                    return Outcome.BLOCKED            # the real pane: queued input blocks
                return super().type_row(first_line, body)
        shared = []

        def one_pane(path):
            if not shared:
                shared.append(Queuing(path, context_home=self.home))
            return shared[0]
        r = self.runner(pane=one_pane)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "queued in the CLI", sender="Wren"))
        self.assertTrue(_wait(lambda: shared and shared[0].typed))
        r.stop(timeout=3)                              # unheld: the pane lives on
        self.hook_record(r, shared[0])
        r2 = self.runner(pane=one_pane)
        r2.start()
        self.assertTrue(_wait(lambda: r2.state() == "idle" and r2._path is not None))
        time.sleep(0.3)
        first = shared[0].typed[0][0]
        self.write(r2, {"type": "user", "promptSource": "queued", "promptId": "pq",
                        "message": {"role": "user", "content": first}})
        shared[0].holding = False
        self.write(r2, {"type": "system", "subtype": "turn_duration", "durationMs": 1})
        self.assertTrue(_wait(lambda: self.outcome(r2, rec)[1] == "delivered"))
        time.sleep(0.3)
        self.assertEqual(len(shared[0].typed), 1, "never typed twice")
        self.assertNotIn("foreign_turn", self.kinds(r2))

    def test_a_stranded_paste_in_an_adopted_pane_is_cleared_never_entered(self):
        class Stranded(FakePane):
            box = "[Pasted text #1 +3 lines]"

            def box_text(self):
                return self.box

            def clear(self):
                super().clear()
                self.box = ""
        shared = []

        def one_pane(path):                 # the second runner adopts the first's pane
            if not shared:
                shared.append(Stranded(path))
            return shared[0]
        r = self.runner(pane=one_pane)
        r.start()
        self.assertTrue(_wait(lambda: shared and shared[0].alive()))
        r.stop(timeout=3)                   # unheld: the pane lives on
        self.assertTrue(shared[0].alive())
        self.hook_record(r, shared[0])
        r2 = self.runner(pane=one_pane)
        r2.start()
        self.assertTrue(_wait(lambda: "C-u" in shared[0].keys))
        self.assertEqual(shared[0].box, "")
        self.assertEqual(shared[0].typed, [], "nothing entered on top of the stranded text")


class TestLimits(Case):
    def test_a_limit_end_requeues_the_taken_row_and_the_runner_waits(self):
        self.on_prompt = lambda pane, first, body: "limit"
        r = self.runner()
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "over the limit", sender="Wren"))
        self.assertTrue(_wait(lambda: r.state() == "rate_limited"))
        self.assertEqual(self.outcome(r, rec)[0], "queued", "requeued, never failed")
        time.sleep(0.3)
        self.assertEqual(len(self.panes[0].typed), 1, "not retyped inside the limit window")


def _handoff_tool(pane, first_line, body):
    """The model's side of the handoff turn: the tool writes data/handoff.md last."""
    if "`handoff` tool" in first_line + body:
        home = pane.transcript.parents[3]
        (home / "data" / "handoff.md").write_text("# Handoff\n\ndegraded_state: false\n")


class TestRollover(Case):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        (self.home / ".cfg").mkdir()

    def roll(self, r):
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        old = r.session_id()
        out = r.rollover("contract")
        self.assertTrue(out["ok"], out)
        row = r.inbox.get(out["inbox_id"])
        return old, row, json.loads(row["detail"] or "{}")

    def test_a_clean_handoff_exits_the_old_cli_and_starts_the_new_id_fresh(self):
        self.on_prompt = _handoff_tool
        r = self.runner(handoff_deadline_s=5, settle_s=0.5)   # /exit meets a CLI still busy
        old, row, detail = self.roll(r)
        self.assertEqual((row["outcome"], detail["handoff"], detail["exit"]), ("delivered", "clean", "exit"))
        self.assertEqual(self.panes[0].exits, 1)
        self.assertEqual(self.panes[0].kills, 0, "the old CLI ended by /exit, not a kill")
        self.assertTrue(self.panes[-1].alive(), "the new pane found its context block")
        self.assertEqual(self.panes[-1].launch_refused, 0)
        new = r.session_id()
        self.assertNotEqual(new, old)
        self.assertEqual(self.panes[-1].started[-1][0], ["claude", new, "--fresh"])
        saved = json.loads((self.home / "data" / "runner-session.json").read_text())
        self.assertEqual(saved["session_id"], new)
        self.assertIn("`handoff` tool", self.panes[0].typed[0][0] + self.panes[0].typed[0][1])
        finals = [e["payload"] for e in r.events() if e["kind"] == "extract" and e["payload"].get("final")]
        self.assertEqual([e["session_id"] for e in finals], [old], "a final mine of the old session")

    def test_the_session_record_names_the_rollovers_generation(self):
        """runner-session.json once kept the old
        generation (written before the bump) until the new session's first
        turn; a kind switch or a reader in that window saw 0 against the
        rollover's 1. It is the rollover's generation when the row closes."""
        from cousin_lib import boot
        self.on_prompt = _handoff_tool
        r = self.runner(handoff_deadline_s=5, settle_s=0.5)
        at_done = {}
        real_append = r.stream.append

        def append(kind, payload):
            if kind == "rollover" and payload.get("phase") == "done":
                at_done.update(json.loads((self.home / "data" / "runner-session.json").read_text()))
            return real_append(kind, payload)
        r.stream.append = append
        before = boot.read_generation(self.home)
        _old, row, detail = self.roll(r)
        self.assertEqual(detail["generation"], before + 1)
        self.assertEqual(at_done["generation"], detail["generation"])
        self.assertEqual(at_done["session_id"], detail["session_id"])
        self.assertTrue(at_done.get("fresh"), "still fresh until the CLI writes the session")

    def test_a_turn_without_the_handoff_writes_an_emergency_handoff(self):
        r = self.runner(handoff_deadline_s=5)
        _old, row, detail = self.roll(r)
        self.assertEqual((row["outcome"], detail["handoff"]), ("delivered", "emergency"))
        archived = sorted((self.home / "data" / "generations").glob("gen-*/handoff.md"))
        self.assertTrue(archived)
        self.assertIn("degraded_state: true", archived[-1].read_text())
        self.assertIn("without calling handoff", archived[-1].read_text())

    def test_a_usage_limit_on_the_handoff_turn_postpones_the_rollover(self):
        self.on_prompt = lambda pane, first, body: "limit" if "`handoff` tool" in first + body else None
        r = self.runner(handoff_deadline_s=5)
        from cousin_lib import boot
        gen = boot.read_generation(self.home)
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
        old = r.session_id()
        from cousin_lib.runner import rollover, wake
        inbox_id, _ = rollover.put_once(r.inbox, self.home, "contract")   # r.rollover() would wait 30 s
        wake.poke(self.home)
        self.assertTrue(_wait(lambda: any(e["kind"] == "rollover" and e["payload"].get("phase") == "postponed"
                                          for e in r.events())))
        row = r.inbox.get(inbox_id)
        self.assertEqual(row["state"], "queued", "the row waits, never an emergency")
        self.assertEqual((r.session_id(), boot.read_generation(self.home)), (old, gen))
        self.assertFalse((self.home / "data" / "handoff.md").exists())

    def test_a_recorded_id_whose_cli_never_started_starts_fresh_under_it(self):
        sid = "5e55a000-0000-4000-8000-000000000001"
        (self.home / "data").mkdir(exist_ok=True)
        (self.home / "data" / "runner-session.json").write_text(json.dumps(
            {"session_id": sid, "kind": "tmux", "fresh": True}))
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].started))
        self.assertEqual(self.panes[0].started[0][0], ["claude", sid, "--fresh"])

    def test_the_fresh_mark_is_dropped_once_the_cli_has_written_the_session(self):
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: (self.home / "data" / "runner-session.json").exists()))
        saved = lambda: json.loads((self.home / "data" / "runner-session.json").read_text())
        self.assertTrue(saved().get("fresh"))
        rec = r.enqueue(Item("operator:priya", "chat", "hello", sender="priya"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[0] == "done"))
        self.assertNotIn("fresh", saved())
        self.assertEqual(saved()["session_id"], r.session_id())

    def test_a_normal_turn_end_mines_the_transcript(self):
        r = self.runner()
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hello", sender="priya"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[0] == "done"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "extract" for e in r.events())))
        ex = [e["payload"] for e in r.events() if e["kind"] == "extract"][0]
        self.assertEqual((ex["session_id"], ex["turn"]), (r.session_id(), 1))
        self.assertGreaterEqual(ex["written"], 0)


class TestMiningCursor(HermeticCase):
    def test_a_cursor_moved_to_the_end_mines_nothing_already_there(self):
        """After a switch the session keeps its id and the mining
        cursor moves to the end of the target's transcript."""
        from cousin_lib.runner import extract, transcript
        home = temp_home(self)
        path = home / "t.jsonl"
        said = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "The tmux kind decided that the runner owns the cursor file."}]}})
        path.write_text(said + "\n")
        store = transcript.TranscriptStore(path, "sid-1")
        extract.set_cursor(home, "sid-1", path.stat().st_size)
        self.assertEqual(extract.mine_turn(home, "sid-1", 1, store=store), 0)
        self.assertEqual(extract._load(home)["sid-1"], path.stat().st_size)


if __name__ == "__main__":
    unittest.main()


class TestResultBeforeRows(Case):
    """The result is appended before the rows close, and the
    rows close even when that append raises."""

    def claimed(self, r, body="hi"):
        rid = r.inbox.put(Item("operator:wren", "chat", body, sender="Wren"))
        return r.inbox.claim_id(rid, claimant=r.runner_id)

    def test_a_refused_row_is_failed_even_when_its_result_cannot_be_written(self):
        class Dead(FakePane):
            def type_row(self, first_line, body):
                return Outcome.FAILED
        r = self.runner(pane=lambda path: Dead(path))
        said = _result_append_fails_once(r)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "hi", sender="Wren"))
        self.assertTrue(_wait(lambda: said))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[0] == "done"), "left claimed")
        self.assertEqual(self.outcome(r, rec)[1], "failed")

    def test_a_prompt_never_taken_is_failed_even_when_its_result_cannot_be_written(self):
        r = self.runner()
        row = self.claimed(r)
        r.pane = FakePane(self.home / "t.jsonl")          # never started: empty box, no queue
        r.pane.queued = lambda: False
        r.pane.box_text = lambda: ""
        r._attempts[row["id"]] = 1                         # typed once already
        r._claims[row["id"]] = {"row": row, "typed_at": 0.0, "nonces": ["n1"], "taken": None}
        _result_append_fails_once(r)
        with self.assertRaises(OSError):
            r._check_consumed([r._claims[row["id"]]])
        self.assertEqual((r.inbox.get(row["id"])["state"], r.inbox.get(row["id"])["outcome"]),
                         ("done", "failed"))

    def test_a_skipped_turn_end_closes_its_rows_even_when_its_result_cannot_be_written(self):
        from types import SimpleNamespace
        r = self.runner()
        row = self.claimed(r)
        r.machine.to("running", "turn")
        r._live = {"rows": [row], "prompt_id": "p1", "who": "runner"}
        _result_append_fails_once(r)
        e = SimpleNamespace(kind="turn_end", offset=7)
        with self.assertRaises(OSError):
            r._close_skipped(e, 4, RuntimeError("scripted"))
        self.assertEqual((r.inbox.get(row["id"])["state"], r.inbox.get(row["id"])["outcome"]),
                         ("done", "delivered"))
        self.assertIsNone(r._live)
        self.assertEqual(r.state(), "idle")
