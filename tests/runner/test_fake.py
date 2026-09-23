"""FakeRunner beyond the contract: state transitions and stop."""
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _wait(pred, timeout=5.0, step=0.02):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


class _RaisesOnceRunner(FakeRunner):
    """A FakeRunner whose first turn blows up, to prove a raising turn is
    recorded rather than silently killing the worker thread."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._raised = False

    def _turn(self, first):
        if not self._raised:
            self._raised = True
            raise RuntimeError("boom")
        return super()._turn(first)


class TestFakeRunner(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def test_state_transitions_are_in_the_stream(self):
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        time.sleep(0.3)
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertEqual(states[:2], ["running", "idle"])

    def test_stop_is_terminal_and_idempotent(self):
        r = FakeRunner(self.home)
        r.start(); r.stop(timeout=5); r.stop(timeout=5)
        self.assertEqual(r.state(), "stopped")

    def test_rollover_declares_itself_not_implemented_in_this_phase(self):
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        out = r.rollover("test")
        self.assertEqual(out["ok"], False)
        self.assertIn("phase 4", out["reason"])

    def test_a_turn_that_raises_is_recorded_and_the_runner_keeps_going(self):
        r = _RaisesOnceRunner(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        first = r.enqueue(Item("operator:priya", "chat", "boom", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "error" for e in r.events())))
        self.assertTrue(_wait(lambda: r.state() == "idle"))

        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertIn("errored", states)
        self.assertIn("idle", states)
        self.assertLess(states.index("errored"), states.index("idle"))

        row = r.inbox.get(first.inbox_id)
        self.assertEqual(row["outcome"], "failed")

        r.enqueue(Item("operator:priya", "chat", "ok", sender="Priya"))
        self.assertTrue(_wait(lambda: any(
            e["kind"] == "result" and e["payload"].get("is_error") is False
            for e in r.events())))
        self.assertEqual(r.state(), "idle")


if __name__ == "__main__":
    unittest.main()
