"""FakeRunner beyond the contract: state transitions and stop."""
import os
import threading
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner import wake
from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _wait(pred, timeout=10.0, step=0.02):
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


class _RaisesAfterStopRunner(FakeRunner):
    """A FakeRunner whose first fold announces itself (`fold_entered`),
    blocks until the test opens `release`, then raises - so the test can
    force `stop()` onto the machine while the turn is inside its body and
    only then let the turn fail. Proves `_fail_turn` never attempts an
    illegal transition out of `stopped`.

    Gates, not a sleep: `state() == "running"` flips before the turn has
    reached its first fold, and a stop that lands in that window is an
    interrupt the turn honours by folding nothing (no fold after an
    interrupt is by design), so it ends in a clean interrupted `result`
    and never raises. The test must wait for the fold itself."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._raised = False
        self.fold_entered = threading.Event()
        self.release = threading.Event()

    def _fold_midturn(self, consumed):
        if not self._raised:
            self._raised = True
            self.fold_entered.set()
            self.release.wait(10)
            raise RuntimeError("boom-after-stop")
        return super()._fold_midturn(consumed)


class _RaisesOnFirstResultRunner(FakeRunner):
    """A FakeRunner whose very first "result" stream event raises,
    simulating a failure in the turn's success TAIL. The result is appended
    before the rows close, and the rows close `delivered` even so.
    Proves that failure does not re-close those rows as `failed`
    (`Inbox.done` has no re-close guard)."""

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


class _RecordsFoldsRunner(FakeRunner):
    """Counts the claims a fold makes after an interrupt was asked."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.folds_after_interrupt = 0
        self._in_fold = False
        real_claim = self.inbox.claim

        def claim(**kw):
            if self._in_fold and self._interrupt.is_set():
                self.folds_after_interrupt += 1
            return real_claim(**kw)
        self.inbox.claim = claim

    def _fold_midturn(self, consumed):
        self._in_fold = True
        try:
            return super()._fold_midturn(consumed)
        finally:
            self._in_fold = False


class TestFakeRunner(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def test_state_transitions_are_in_the_stream(self):
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        # wait for the idle event, not a fixed sleep: under load the turn
        # can outlast 0.3 s and the list read short
        self.assertTrue(_wait(lambda: any(e["kind"] == "state" and e["payload"]["to"] == "idle"
                                          for e in r.events())))
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertEqual(states[:2], ["running", "idle"])

    def test_stop_is_terminal_and_idempotent(self):
        r = FakeRunner(self.home)
        r.start(); r.stop(timeout=5); r.stop(timeout=5)
        self.assertEqual(r.state(), "stopped")

    def test_rollover_on_a_runner_not_started_is_an_honest_no_and_a_durable_row(self):
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        out = r.rollover("test")
        self.assertEqual(out["ok"], False)
        self.assertIn("not running", out["reason"])
        self.assertEqual(r.inbox.get(out["inbox_id"])["state"], "queued")

    def test_a_plain_duplicate_flip_row_is_closed_with_the_first_ones_answer(self):
        import json
        from cousin_lib import boot
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        a = r.inbox.put(Item("system", "flip", "max_age", sender="runner"))
        b = r.inbox.put(Item("system", "flip", "max_age", sender="runner"))
        r.start()
        self.assertTrue(_wait(lambda: r.inbox.get(b)["state"] == "done"))
        self.assertEqual(json.loads(r.inbox.get(b)["detail"])["coalesced_into"], a)
        self.assertEqual(r.inbox.get(a)["outcome"], "delivered")
        self.assertEqual(boot.read_generation(self.home), 1)

    def test_a_flip_row_claimed_while_not_idle_goes_back_to_the_queue(self):
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        rid = r.inbox.put(Item("system", "flip", "max_age", sender="runner"))
        [row] = r.inbox.claim(limit=1)
        r.machine.to("running")                   # a live turn: the row must not start
        r._rollover_row(row)
        self.assertEqual(r.inbox.get(rid)["state"], "queued")
        self.assertEqual(r.state(), "running")

    def test_a_turn_that_raises_is_recorded_and_the_runner_keeps_going(self):
        r = _RaisesOnceRunner(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        first = r.enqueue(Item("operator:priya", "chat", "boom", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "error" for e in r.events())))
        # the machine's state flips before its `state` event is appended:
        # wait for the event, not the attribute
        self.assertTrue(_wait(lambda: any(e["kind"] == "state" and e["payload"]["to"] == "idle"
                                          and e["payload"]["from"] == "errored"
                                          for e in r.events())))

        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertIn("errored", states)
        self.assertIn("idle", states)
        self.assertLess(states.index("errored"), states.index("idle"))

        row = r.inbox.get(first.inbox_id)
        self.assertEqual(row["outcome"], "failed")

        r.enqueue(Item("operator:priya", "chat", "ok", sender="Priya"))
        # the result is appended, then the row closes and the machine idles
        self.assertTrue(_wait(lambda: any(
            e["kind"] == "result" and e["payload"].get("is_error") is False
            for e in r.events()) and r.state() == "idle"))
        self.assertEqual(r.state(), "idle")

    def test_a_turn_that_raises_after_stop_never_touches_a_stopped_machine(self):
        r = _RaisesAfterStopRunner(self.home, turn_seconds=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        self.addCleanup(r.release.set)          # runs first (LIFO): never stop a worker parked on the gate
        r.start()
        r.enqueue(Item("operator:priya", "chat", "slow-boom", sender="Priya"))
        # the turn is inside its body, not merely `running` (see the runner's docstring)
        self.assertTrue(r.fold_entered.wait(10))

        r.stop(timeout=0.01)  # the worker is parked on the gate: this cannot join it
        self.assertEqual(r.state(), "stopped")
        r.release.set()       # only now does the turn raise, against a stopped machine

        r._thread.join(10)
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
        # the result is appended, then the row closes and the machine idles
        self.assertTrue(_wait(lambda: any(
            e["kind"] == "result" and e["payload"].get("is_error") is False
            for e in r.events()) and r.state() == "idle"))
        self.assertEqual(r.state(), "idle")

    def test_errored_is_in_the_stream_before_the_error_event(self):
        r = _RaisesOnceRunner(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "boom", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "error" for e in r.events())))
        events = list(r.events())
        errored = next(e["seq"] for e in events
                       if e["kind"] == "state" and e["payload"]["to"] == "errored")
        error = next(e["seq"] for e in events if e["kind"] == "error")
        self.assertLess(errored, error)

    def test_nothing_is_folded_once_an_interrupt_is_asked(self):
        r = _RecordsFoldsRunner(self.home, turn_seconds=5.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "slow", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertEqual(r.folds_after_interrupt, 0)

    def test_a_scripted_failure_fails_once_then_the_runner_works(self):
        r = FakeRunner(self.home, script=["fail_once"])
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "a", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(a.inbox_id)["state"] == "done"))
        b = r.enqueue(Item("operator:priya", "chat", "b", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "done"))
        self.assertEqual([r.inbox.get(i.inbox_id)["outcome"] for i in (a, b)],
                         ["failed", "delivered"])

    def test_a_wake_socket_that_cannot_bind_falls_back_to_polling(self):
        run = self.home / "run"
        os.rmdir(run)
        run.write_text("not a directory")
        r = FakeRunner(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(a.inbox_id)["state"] == "done"))
        errors = [e["payload"]["error"] for e in r.events() if e["kind"] == "error"]
        self.assertTrue(any(str(wake.socket_path(self.home)) in e for e in errors))
        self.assertTrue(r.worker_alive())

    # -- Turn ----------------------------------------------------------------
    def test_the_turn_carries_both_threads_of_a_fold(self):
        r = FakeRunner(self.home, turn_seconds=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        seen = {}
        real_append = r.stream.append
        def spy_append(kind, payload):
            if kind == "result":
                seen["threads"] = r.turn.threads
            return real_append(kind, payload)
        r.stream.append = spy_append
        r.start()
        r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r.enqueue(Item("person:sam", "chat", "second", sender="Sam"))
        self.assertTrue(_wait(lambda: "threads" in seen, timeout=6))
        self.assertEqual(set(seen["threads"]), {"operator:priya", "person:sam"})
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertFalse(r.turn.active)


if __name__ == "__main__":
    unittest.main()
