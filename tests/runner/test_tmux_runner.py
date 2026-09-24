"""The tmux kind's rules beyond the contract suite (phase 11 R4, R6, R6b,
R6c, R21, R23): what closes a row and when, what a turn start nobody typed
is, what a restart does with rows already typed, and the screens the runner
never types into."""
import json
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.tmux_pane import Outcome
from cousin_lib.runner.tmux_runner import TmuxRunner
from tests._hermetic import HermeticCase
from tests.runner._fake_pane import FakePane
from tests.runner._home import temp_home


def _wait(pred, timeout=5.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Case(HermeticCase):
    def runner(self, pane=None, **kw):
        self.home = getattr(self, "home", None) or temp_home(self)
        panes = []

        def factory(path):
            p = pane(path) if pane else FakePane(path, on_prompt=getattr(self, "on_prompt", None), **kw)
            panes.append(p)
            return p
        deadline = kw.pop("handoff_deadline_s", None)
        r = TmuxRunner(self.home, account=None, pane_factory=factory, config_dir=self.home / ".cfg",
                       handoff_deadline_s=deadline,
                       launch_argv=lambda sid, fresh: ["claude", sid] + (["--fresh"] if fresh else []))
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
        """What the pane's SessionStart hook writes (tmux_hook, R24): the
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
        self.assertEqual(self.outcome(r, rec)[0], "queued")
        self.assertEqual(self.panes[0].typed, [])
        self.assertTrue((self.home / "data" / "login-required.json").exists())

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
        self.assertTrue(_wait(lambda: self.panes[0].typed))
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


class TestTurns(Case):
    def test_a_turn_start_nobody_typed_is_a_foreign_turn(self):
        r = self.runner()
        r.start()
        self.assertTrue(_wait(lambda: r._path is not None and r._path.exists()))
        self.write(r, {"type": "user", "promptSource": "typed", "promptId": "px",
                       "message": {"role": "user", "content": "someone at the keyboard"}})
        self.assertTrue(_wait(lambda: "foreign_turn" in self.kinds(r)))
        self.assertEqual(r.state(), "running")
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
        results = [e["payload"] for e in r.events() if e["kind"] == "result"]
        self.assertTrue(results[0]["interrupted"])

    def test_a_closed_nonce_seen_again_is_a_duplicate(self):
        r = self.runner()
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "once", sender="Wren"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        nonce = self.panes[0].typed[0][0][len("[inbox:"):len("[inbox:") + 12]
        self.write(r, {"type": "user", "promptSource": "typed", "promptId": "pd",
                       "message": {"role": "user", "content": "[inbox:%s] again" % nonce}})
        self.assertTrue(_wait(lambda: "duplicate_delivery" in self.kinds(r)))


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


class TestRecovery(Case):
    """R23: a second runner on the same home settles what the first typed."""

    def first_runner_types(self, **kw):
        r = self.runner(**kw)
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "typed before the restart", sender="Wren"))
        self.assertTrue(_wait(lambda: self.panes[0].typed))
        return r, rec

    def test_a_row_taken_and_ended_while_no_runner_watched_closes_delivered_once(self):
        r, rec = self.first_runner_types(slow=True)
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r._stop.set()                                  # the runner dies; the CLI finishes the turn
        r._thread.join(3)
        path = r._path
        self.assertTrue(_wait(lambda: "turn_duration" in path.read_text(), timeout=6))
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

    def test_an_untaken_row_is_requeued_and_typed_again(self):
        class Deaf(FakePane):
            def _play(self, first_line, body, n):     # typed, never taken
                pass
        r = self.runner(pane=lambda path: Deaf(path))
        r.start()
        rec = r.enqueue(Item("operator:wren", "chat", "lost in the box", sender="Wren"))
        self.assertTrue(_wait(lambda: self.panes[0].typed))
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
        self.assertTrue(_wait(lambda: self.panes[0].typed))
        first = self.panes[0].typed[0][0]
        with r._path.open("a") as fh:
            fh.write('{"type": "assistant", "mess')          # torn by a SIGKILL
            fh.write(json.dumps({"type": "user", "promptSource": "typed", "promptId": "p1",
                                 "message": {"role": "user", "content": first}}) + "\n")
            fh.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
        self.assertTrue(_wait(lambda: self.outcome(r, rec)[1] == "delivered"))
        time.sleep(0.3)
        self.assertEqual(len(self.panes[0].typed), 1, "never typed twice")

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
        new = r.session_id()
        self.assertNotEqual(new, old)
        self.assertEqual(self.panes[-1].started[-1][0], ["claude", new, "--fresh"])
        saved = json.loads((self.home / "data" / "runner-session.json").read_text())
        self.assertEqual(saved["session_id"], new)
        self.assertIn("`handoff` tool", self.panes[0].typed[0][0] + self.panes[0].typed[0][1])
        finals = [e["payload"] for e in r.events() if e["kind"] == "extract" and e["payload"].get("final")]
        self.assertEqual([e["session_id"] for e in finals], [old], "a final mine of the old session")

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
        self.assertTrue(_wait(lambda: self.panes and self.panes[0].alive()))
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
        """R17: after a switch the session keeps its id and the mining
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
