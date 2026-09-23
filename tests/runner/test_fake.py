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
    """A FakeRunner whose first turn's BODY blows up (inside `_turn`'s own
    try, same as it is meant to catch), to prove a raising turn is
    recorded rather than silently killing the worker thread. Raising
    inside `_fold_midturn` rather than by overriding `_turn` wholesale
    matters here: `_turn`'s own try/except is what owns the row in
    `consumed` and marks it failed; an override that raises before ever
    reaching that try exercises a different, deliberately more cautious
    path (see `test_a_failure_after_rows_are_closed_does_not_reclose_them`
    and `_fail_turn`'s docstring for why that path does not touch rows it
    cannot vouch for)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._raised = False

    def _fold_midturn(self, consumed):
        if not self._raised:
            self._raised = True
            raise RuntimeError("boom")
        return super()._fold_midturn(consumed)


class _RaisesAfterDelayRunner(FakeRunner):
    """A FakeRunner whose first fold sleeps briefly, then raises - long
    enough for a concurrent `stop()` to force the state machine to
    `stopped` before the turn's own failure path runs. Proves
    `_fail_turn` never attempts an illegal transition out of `stopped`."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._raised = False

    def _fold_midturn(self, consumed):
        if not self._raised:
            self._raised = True
            time.sleep(0.4)
            raise RuntimeError("boom-after-stop")
        return super()._fold_midturn(consumed)


class _RaisesOnFirstResultRunner(FakeRunner):
    """A FakeRunner whose very first "result" stream event raises,
    simulating a failure in the turn's success TAIL - after its rows are
    already closed `delivered`. Proves that failure does not re-close
    those rows as `failed` (`Inbox.done` has no re-close guard)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._raised_on_result = False
        self._real_append = self.stream.append
        self.stream.append = self._append

    def _append(self, kind, payload):
        if kind == "result" and not self._raised_on_result:
            self._raised_on_result = True
            raise RuntimeError("boom-in-result-append")
        return self._real_append(kind, payload)


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

    def test_a_turn_that_raises_after_stop_never_touches_a_stopped_machine(self):
        r = _RaisesAfterDelayRunner(self.home, turn_seconds=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "slow-boom", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))

        r.stop(timeout=0.01)  # returns long before the 0.4s fold-and-raise finishes
        self.assertEqual(r.state(), "stopped")

        r._thread.join(3)
        self.assertFalse(r._thread.is_alive(), "the worker thread must not die uncaught")
        self.assertEqual(r.state(), "stopped")

        kinds = [e["kind"] for e in r.events()]
        text = " ".join(str(e) for e in r.events())
        self.assertIn("error", kinds)
        self.assertNotIn("IllegalTransition", text)

    def test_a_failure_after_rows_are_closed_does_not_reclose_them(self):
        r = _RaisesOnFirstResultRunner(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        first = r.enqueue(Item("operator:priya", "chat", "ok-then-boom", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "error" for e in r.events())))
        self.assertTrue(_wait(lambda: r.state() == "idle"))

        row = r.inbox.get(first.inbox_id)
        self.assertEqual(row["outcome"], "delivered")

        r.enqueue(Item("operator:priya", "chat", "second", sender="Priya"))
        self.assertTrue(_wait(lambda: any(
            e["kind"] == "result" and e["payload"].get("is_error") is False
            for e in r.events())))
        self.assertEqual(r.state(), "idle")


if __name__ == "__main__":
    unittest.main()
