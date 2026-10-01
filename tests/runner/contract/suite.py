"""THE contract suite: one suite, every runner.

A concrete case mixes this in, subclasses HermeticCase, and defines
`make_runner(home, *, slow=False, fail_first=False)`:
  slow        the FIRST turn stays open about 3 s, long enough for a test
              to act while it runs, and ends at once when interrupted
  fail_first  the FIRST turn fails (its row closes `failed`); the runner
              recovers and every later turn works
Every runner the framework ships passes this file, and `unsupported()`
is the only permitted way to opt out of an item: by declaring it, never
by failing it quietly. Each test names its item with `@item`, and a
runner declaring that item unsupported skips exactly that test.
"""
import time

from cousin_lib.delivery import Item
from cousin_lib.runner.base import Receipt
from tests.runner._home import temp_home

CONTRACT_ITEMS = ("enqueue_receipt", "priority_order", "consume_after_start",
                  "interrupt_ends_turn", "turn_events", "unsupported_list",
                  "midturn_fold", "outcome_delivered", "outcome_failed",
                  "outcome_interrupted", "failure_recovers", "stop_ends_turn",
                  "loop_waits", "state_events", "events_after",
                  "interrupt_idle_false", "enqueue_type_error", "rollover_shape",
                  "rollover_generation", "interrupt_row", "interrupt_row_idle")


def item(name):
    """Mark a contract test with the item it proves; `_runner()` skips it
    when the runner declares that item unsupported."""
    def mark(fn):
        def test(self):
            self._item = name
            return fn(self)
        test.__name__, test.__doc__ = fn.__name__, fn.__doc__
        test.contract_item = name
        return test
    return mark


def _wait(pred, timeout=5.0, step=0.02):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def _kinds(runner):
    return [e["kind"] for e in runner.events()]


def _results(runner):
    # a `drained: True` result is a failed turn's leftover, not a turn
    return [e["payload"] for e in runner.events()
            if e["kind"] == "result" and not e["payload"].get("drained")]


def _op(body):
    return Item("operator:priya", "chat", body, sender="Priya")


def _interrupt():
    return Item("system", "interrupt", "interrupt asked from the console", sender="Priya")


class RunnerContract:
    _item = None

    def make_runner(self, home, *, slow=False, fail_first=False):
        raise NotImplementedError

    def _runner(self, **kw):
        self.home = temp_home(self)
        r = self.make_runner(self.home, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        if self._item is not None and self._item in r.unsupported():
            self.skipTest("runner declares %s unsupported" % self._item)
        return r

    def _row_outcome(self, r, receipt):
        row = r.inbox.get(receipt.inbox_id)
        return row["outcome"] if row and row["state"] == "done" else None

    def _closed_before_result(self, r):
        """The ids whose row was closed before a `result` event named
        them. A turn's result goes on the stream first, so whoever reads the
        row closed also finds the result that closed it."""
        early, real = [], r.inbox.done

        def done(inbox_id, *args, **kwargs):
            if not any(inbox_id in x.get("inbox_ids", ()) for x in _results(r)):
                early.append(inbox_id)
            return real(inbox_id, *args, **kwargs)
        r.inbox.done = done
        return early

    # -- the items -------------------------------------------------------------
    @item("enqueue_receipt")
    def test_enqueue_returns_a_receipt(self):
        r = self._runner()
        receipt = r.enqueue(_op("hi"))
        self.assertIsInstance(receipt, Receipt)
        self.assertGreater(receipt.inbox_id, 0)
        self.assertEqual(receipt.outcome, "queued")

    @item("enqueue_type_error")
    def test_enqueue_refuses_anything_but_an_item(self):
        r = self._runner()
        with self.assertRaises(TypeError):
            r.enqueue("not an item")
        with self.assertRaises(TypeError):
            r.enqueue({"thread_id": "operator:priya", "body": "x"})

    @item("priority_order")
    def test_items_are_consumed_in_priority_order(self):
        r = self._runner()
        # none of these three folds into another's turn (a peer would fold
        # into the operator's), so each is a turn of its own
        r.enqueue(Item("loop:heartbeat", "loop", "loop"))
        r.enqueue(Item("schedule", "schedule", "schedule"))
        r.enqueue(_op("op"))
        r.start()
        self.assertTrue(_wait(lambda: len(_results(r)) >= 3))
        bodies = [e["payload"]["bodies"][0] for e in r.events() if e["kind"] == "turn_start"]
        self.assertEqual(bodies, ["op", "schedule", "loop"])

    @item("consume_after_start")
    def test_an_item_put_while_stopped_is_consumed_after_start(self):
        r = self._runner()
        r.enqueue(_op("later"))
        time.sleep(0.1)
        self.assertNotIn("result", _kinds(r))
        r.start()
        self.assertTrue(_wait(lambda: "result" in _kinds(r)))

    @item("outcome_delivered")
    def test_a_finished_turn_closes_its_row_delivered(self):
        r = self._runner()
        early = self._closed_before_result(r)
        r.start()
        a = r.enqueue(_op("one"))
        # the result naming the row is on the stream before the row closes
        self.assertTrue(_wait(lambda: self._row_outcome(r, a) is not None))
        self.assertNotIn(a.inbox_id, early)
        self.assertEqual(self._row_outcome(r, a), "delivered")
        self.assertEqual(_results(r)[-1]["inbox_ids"], [a.inbox_id])
        self.assertFalse(_results(r)[-1]["is_error"])

    @item("outcome_failed")
    def test_a_failed_turn_closes_its_row_failed(self):
        r = self._runner(fail_first=True)
        early = self._closed_before_result(r)
        r.start()
        a = r.enqueue(_op("one"))
        # the result naming the row is on the stream before the row closes
        self.assertTrue(_wait(lambda: self._row_outcome(r, a) is not None))
        self.assertNotIn(a.inbox_id, early)
        self.assertEqual(self._row_outcome(r, a), "failed")
        failed = [x for x in _results(r) if a.inbox_id in x["inbox_ids"]]
        self.assertTrue(failed and failed[0]["is_error"])

    @item("failure_recovers")
    def test_a_failure_is_recorded_and_the_next_row_still_runs(self):
        r = self._runner(fail_first=True)
        r.start()
        a = r.enqueue(_op("one"))
        self.assertTrue(_wait(lambda: self._row_outcome(r, a) is not None))
        # the `state` event lands just after the attribute flips: wait for it
        self.assertTrue(_wait(lambda: any(e["kind"] == "state" and e["payload"]["from"] == "errored"
                                          for e in r.events()), timeout=8))
        self.assertIn("error", _kinds(r))
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertIn("errored", states)
        self.assertEqual(states[states.index("errored") + 1], "idle")
        b = r.enqueue(_op("two"))
        self.assertTrue(_wait(lambda: self._row_outcome(r, b) is not None, timeout=8))
        self.assertEqual(self._row_outcome(r, b), "delivered")

    @item("interrupt_ends_turn")
    def test_interrupt_during_a_turn_ends_it_and_returns_to_idle(self):
        r = self._runner(slow=True)
        r.start()
        r.enqueue(_op("slow"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=3.0))
        self.assertIn("result", _kinds(r))
        self.assertTrue(_results(r)[-1].get("interrupted"))

    @item("outcome_interrupted")
    def test_an_interrupted_turns_row_is_delivered(self):
        r = self._runner(slow=True)
        early = self._closed_before_result(r)
        r.start()
        a = r.enqueue(_op("slow"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: self._row_outcome(r, a) is not None, timeout=3.0))
        self.assertEqual(self._row_outcome(r, a), "delivered", "the model received it")
        # the turn's `result` is on the stream before its row closes
        self.assertNotIn(a.inbox_id, early)
        self.assertTrue(_results(r)[-1]["interrupted"])

    @item("interrupt_idle_false")
    def test_interrupt_with_no_turn_running_is_false(self):
        r = self._runner()
        self.assertFalse(r.interrupt())
        r.start()
        time.sleep(0.1)
        self.assertEqual(r.state(), "idle")
        self.assertFalse(r.interrupt())

    @item("interrupt_row")
    def test_an_interrupt_row_ends_the_live_turn_and_is_delivered(self):
        """The out-of-process interrupt: a process that holds no
        runner object puts an `interrupt` row; the live turn ends, its own
        row is still delivered, and the interrupt row says it landed."""
        r = self._runner(slow=True)
        r.start()
        a = r.enqueue(_op("slow"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        stop = r.enqueue(_interrupt())
        self.assertTrue(_wait(lambda: self._row_outcome(r, stop) is not None, timeout=3.0))
        self.assertEqual(self._row_outcome(r, stop), "delivered")
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=3.0))
        self.assertTrue(_results(r)[-1]["interrupted"])
        self.assertEqual(self._row_outcome(r, a), "delivered")
        self.assertNotIn(stop.inbox_id, _results(r)[-1]["inbox_ids"])

    @item("interrupt_row_idle")
    def test_an_interrupt_row_with_no_turn_running_is_failed_not_a_turn(self):
        r = self._runner()
        r.start()
        stop = r.enqueue(_interrupt())
        self.assertTrue(_wait(lambda: self._row_outcome(r, stop) is not None, timeout=3.0))
        self.assertEqual(self._row_outcome(r, stop), "failed")
        self.assertEqual(r.inbox.get(stop.inbox_id)["detail"], "no turn was running")
        self.assertEqual(_results(r), [])
        self.assertEqual(r.state(), "idle")

    @item("stop_ends_turn")
    def test_stop_during_a_turn_returns_within_its_timeout(self):
        r = self._runner(slow=True)
        r.start()
        r.enqueue(_op("slow"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        t = time.monotonic()
        r.stop(timeout=5)
        self.assertLess(time.monotonic() - t, 2.5, "the slow turn (3 s) was ended, not waited out")
        self.assertEqual(r.state(), "stopped")
        r.stop(timeout=5)
        self.assertEqual(r.state(), "stopped")

    @item("turn_events")
    def test_every_turn_emits_start_tool_and_result(self):
        r = self._runner()
        r.start()
        r.enqueue(_op("go"))
        self.assertTrue(_wait(lambda: "result" in _kinds(r)))
        kinds = _kinds(r)
        self.assertLess(kinds.index("turn_start"), kinds.index("tool"))
        self.assertLess(kinds.index("tool"), kinds.index("result"))

    @item("state_events")
    def test_every_transition_is_a_state_event_in_order(self):
        r = self._runner()
        r.start()
        r.enqueue(_op("one"))
        self.assertTrue(_wait(lambda: "result" in _kinds(r) and r.state() == "idle"))
        r.stop(timeout=5)
        states = [e["payload"] for e in r.events() if e["kind"] == "state"]
        self.assertEqual(states[0]["from"], "idle")
        for before, after in zip(states, states[1:]):
            self.assertEqual(after["from"], before["to"])
        self.assertEqual(states[-1]["to"], "stopped")
        self.assertIn("running", [s["to"] for s in states])

    @item("events_after")
    def test_events_after_n_resumes_exactly(self):
        r = self._runner()
        r.start()
        r.enqueue(_op("one"))
        self.assertTrue(_wait(lambda: "result" in _kinds(r)))
        r.stop(timeout=5)   # nothing appends after this: the snapshot is whole
        everything = list(r.events())
        self.assertGreater(len(everything), 2)
        for k in (0, len(everything) // 2, len(everything) - 1):
            after = list(r.events(after=everything[k]["seq"]))
            self.assertEqual(after, everything[k + 1:])
        self.assertEqual(list(r.events(after=None)), everything)

    @item("unsupported_list")
    def test_unsupported_lists_only_contract_items(self):
        r = self._runner()
        for name in r.unsupported():
            self.assertIn(name, CONTRACT_ITEMS)
        # plugin_items() is optional: a PLUGIN item is still run by
        # this suite, so it can never also be declared unsupported
        plugin = getattr(r, "plugin_items", lambda: [])()
        for name in plugin:
            self.assertIn(name, CONTRACT_ITEMS)
        self.assertFalse(set(plugin) & set(r.unsupported()))

    @item("rollover_shape")
    def test_rollover_answers_ok_and_a_reason(self):
        r = self._runner()
        out = r.rollover("contract")
        self.assertIsInstance(out, dict)
        self.assertIsInstance(out.get("ok"), bool)
        self.assertIsInstance(out.get("reason"), str)

    @item("midturn_fold")
    def test_a_midturn_operator_or_peer_message_is_closed_by_the_same_result(self):
        """Operator chat and a peer's (a cousin's STOP) reach a
        running turn, not the turn after it."""
        r = self._runner(slow=True)
        r.start()
        first = r.enqueue(_op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        second = r.enqueue(_op("second, mid-turn"))
        peer = r.enqueue(Item("peer:testa", "chat", "peer, mid-turn", sender="Testa"))
        self.assertTrue(_wait(lambda: "result" in _kinds(r), timeout=8.0))
        time.sleep(0.3)
        results = _results(r)
        self.assertEqual(len(results), 1, "one result closes all three")
        self.assertEqual(sorted(results[0]["inbox_ids"]),
                         sorted([first.inbox_id, second.inbox_id, peer.inbox_id]))

    @item("loop_waits")
    def test_a_loop_row_put_mid_turn_waits_for_its_own_turn(self):
        r = self._runner(slow=True)
        r.start()
        first = r.enqueue(_op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        beat = r.enqueue(Item("loop:heartbeat", "loop", "beat", sender=""))
        self.assertTrue(_wait(lambda: len(_results(r)) == 2, timeout=10.0))
        self.assertEqual([x["inbox_ids"] for x in _results(r)],
                         [[first.inbox_id], [beat.inbox_id]])

    @item("rollover_generation")
    def test_a_rollover_moves_the_generation_and_loses_no_row(self):
        from cousin_lib import boot
        r = self._runner()
        r.start()
        g0 = boot.read_generation(self.home)
        out = r.rollover("contract")
        self.assertTrue(out["ok"], out)
        self.assertEqual(boot.read_generation(self.home), g0 + 1)
        rec = r.enqueue(_op("after the rollover"))
        self.assertTrue(_wait(lambda: self._row_outcome(r, rec) == "delivered"))
