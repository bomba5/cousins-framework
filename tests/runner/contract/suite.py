"""THE contract suite: one suite, every runner (master plan, Phase 2).

A concrete case mixes this in, subclasses HermeticCase, and defines
`make_runner(home)`. Every runner the framework ships passes this
file, and `unsupported()` is the only permitted way to opt out of an
item: by declaring it, never by failing it quietly.
"""
import time

from cousin_lib.delivery import Item
from cousin_lib.runner.base import Receipt
from tests.runner._home import temp_home

CONTRACT_ITEMS = ("enqueue_receipt", "priority_order", "consume_after_start",
                  "interrupt_ends_turn", "turn_events", "unsupported_list",
                  "midturn_fold")


def _wait(pred, timeout=5.0, step=0.02):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def _kinds(runner):
    return [e["kind"] for e in runner.events()]


class RunnerContract:
    def make_runner(self, home):
        raise NotImplementedError

    def _runner(self, **kw):
        self.home = temp_home(self)
        r = self.make_runner(self.home, **kw) if kw else self.make_runner(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def _skip_if_declared(self, runner, item):
        if item in runner.unsupported():
            self.skipTest("runner declares %s unsupported" % item)

    def test_enqueue_returns_a_receipt(self):
        r = self._runner()
        self._skip_if_declared(r, "enqueue_receipt")
        receipt = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertIsInstance(receipt, Receipt)
        self.assertGreater(receipt.inbox_id, 0)
        self.assertEqual(receipt.outcome, "queued")

    def test_items_are_consumed_in_priority_order(self):
        r = self._runner()
        self._skip_if_declared(r, "priority_order")
        r.enqueue(Item("loop:heartbeat", "loop", "loop"))
        r.enqueue(Item("peer:testa", "chat", "peer", sender="Testa"))
        r.enqueue(Item("operator:priya", "chat", "op", sender="Priya"))
        r.start()
        self.assertTrue(_wait(lambda: _kinds(r).count("result") >= 3))
        bodies = [e["payload"]["bodies"][0] for e in r.events() if e["kind"] == "turn_start"]
        self.assertEqual(bodies, ["op", "peer", "loop"])

    def test_an_item_put_while_stopped_is_consumed_after_start(self):
        r = self._runner()
        self._skip_if_declared(r, "consume_after_start")
        r.enqueue(Item("operator:priya", "chat", "later", sender="Priya"))
        time.sleep(0.1)
        self.assertNotIn("result", _kinds(r))
        r.start()
        self.assertTrue(_wait(lambda: "result" in _kinds(r)))

    def test_interrupt_during_a_turn_ends_it_and_returns_to_idle(self):
        r = self._runner(turn_seconds=2.0)
        self._skip_if_declared(r, "interrupt_ends_turn")
        r.start()
        r.enqueue(Item("operator:priya", "chat", "slow", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=3.0))
        self.assertIn("result", _kinds(r))
        payload = [e for e in r.events() if e["kind"] == "result"][-1]["payload"]
        self.assertTrue(payload.get("interrupted"))

    def test_every_turn_emits_start_tool_and_result(self):
        r = self._runner()
        self._skip_if_declared(r, "turn_events")
        r.start()
        r.enqueue(Item("operator:priya", "chat", "go", sender="Priya"))
        self.assertTrue(_wait(lambda: "result" in _kinds(r)))
        kinds = _kinds(r)
        self.assertLess(kinds.index("turn_start"), kinds.index("tool"))
        self.assertLess(kinds.index("tool"), kinds.index("result"))

    def test_unsupported_lists_only_contract_items(self):
        r = self._runner()
        for name in r.unsupported():
            self.assertIn(name, CONTRACT_ITEMS)

    def test_a_midturn_operator_message_is_closed_by_the_same_result(self):
        r = self._runner(turn_seconds=1.0)
        self._skip_if_declared(r, "midturn_fold")
        r.start()
        first = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        second = r.enqueue(Item("operator:priya", "chat", "second, mid-turn", sender="Priya"))
        self.assertTrue(_wait(lambda: "result" in _kinds(r), timeout=4.0))
        time.sleep(0.2)
        results = [e for e in r.events() if e["kind"] == "result"]
        self.assertEqual(len(results), 1, "one result closes both items (finding 1)")
        self.assertEqual(sorted(results[0]["payload"]["inbox_ids"]),
                         sorted([first.inbox_id, second.inbox_id]))
