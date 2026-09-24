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
            p = pane(path) if pane else FakePane(path, **kw)
            panes.append(p)
            return p
        r = TmuxRunner(self.home, account=None, pane_factory=factory, config_dir=self.home / ".cfg",
                       launch_argv=lambda sid, fresh: ["claude", sid])
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


if __name__ == "__main__":
    unittest.main()
