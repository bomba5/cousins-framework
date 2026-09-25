"""#66: the carried read (a row written into the live turn whose echo had not
come when the CLI's result did) honours stop() and an interrupt, even when
the CLI never takes the row up."""
import time
import unittest

try:
    from claude_agent_sdk import CLIConnectionError
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, _errors, _results, init_msg, result


class DroppingClient(ScriptedClient):
    """A CLI that takes the write of its `drop`-th message and never takes it
    up: no echo and no turn for it, ever (the case #66 names)."""

    def __init__(self, options, scripts, drop):
        super().__init__(options, scripts)
        self.drop = drop

    async def query(self, prompt, session_id="default"):
        if not self.connected or self.ended:
            raise CLIConnectionError("Not connected. Call connect() first.")
        messages = [prompt] if isinstance(prompt, str) else [m async for m in prompt]
        for message in messages:
            if len(self.queries) + 1 == self.drop:
                self.queries.append(message)       # written, and nothing more
                continue

            async def one(m=message):
                yield m
            await super().query(one())


def _wait(pred, timeout=5.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestCarriedRead(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.clients = []

    def runner(self, first_turn, **kw):
        """The first client drops the second message (the fold); any client
        after it (a reconnect) is an ordinary scripted one."""
        def factory(options):
            if not self.clients:
                client = DroppingClient(options, [first_turn], drop=2)
            else:
                client = ScriptedClient(options, [])
            self.clients.append(client)
            return client
        kw.setdefault("idle_timeout_s", 30.0)
        kw.setdefault("drain_timeout_s", 1.0)
        r = SdkRunner(self.home, client_factory=factory, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def op(self, body):
        return Item("operator:priya", "chat", body, sender="Priya")

    def state_of(self, r, receipt):
        return r.inbox.get(receipt.inbox_id)["state"]

    def outcome_of(self, r, receipt):
        row = r.inbox.get(receipt.inbox_id)
        return row["outcome"] if row["state"] == "done" else None

    def carried(self, r):
        """Start a turn, fold a second row the CLI drops, let the first CLI
        turn end: the runner is now in the carried read, waiting for an echo
        that never comes. Returns the two receipts."""
        r.start()
        a = r.enqueue(self.op("first"))
        self.assertTrue(_wait(lambda: self.clients and self.clients[0].paused))
        b = r.enqueue(self.op("second, folded"))
        self.assertTrue(_wait(lambda: len(self.clients[0].queries) == 2))
        self.clients[0].resume()
        self.assertTrue(_wait(lambda: self.outcome_of(r, a) == "delivered"
                              and any(a.inbox_id in x["inbox_ids"] for x in _results(r))))
        time.sleep(0.3)
        self.assertEqual(r.state(), "running")          # b is carried, not echoed
        self.assertEqual(self.state_of(r, b), "claimed")
        return a, b

    def test_stop_during_the_carried_read_returns_at_once_and_requeues_the_row(self):
        r = self.runner([init_msg(), "PAUSE", result()])
        _, b = self.carried(r)
        t = time.monotonic()
        r.stop(timeout=10)
        self.assertLess(time.monotonic() - t, 3.0, "stop() waited on an echo that never comes")
        self.assertFalse(r.worker_alive())
        self.assertEqual(r.state(), "stopped")
        # never taken up by the CLI: back to the queue, the next start runs it
        self.assertEqual(self.state_of(r, b), "queued")
        said = [x for x in _results(r) if x.get("requeued")]
        self.assertEqual([(x["requeued"], x["is_error"]) for x in said], [([b.inbox_id], False)])
        self.assertEqual(_errors(r), [], "a stop is not a failure")

    def test_an_interrupt_during_the_carried_read_reaches_the_cli_and_ends_the_wait(self):
        r = self.runner([init_msg(), "PAUSE", result()])
        _, b = self.carried(r)
        t = time.monotonic()
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: self.clients[0].interrupts >= 1, 2.0),
                        "the interrupt was dropped, never reached the CLI")
        # the CLI never takes b up: the wait ends within the drain bound, b
        # goes back to the queue and runs as a turn of its own
        self.assertTrue(_wait(lambda: self.outcome_of(r, b) == "delivered", 10.0))
        self.assertLess(time.monotonic() - t, 10.0)
        self.assertNotIn("interrupt_dropped", [e["payload"].get("subtype") for e in r.events()
                                               if e["kind"] == "system"])

    def test_an_interrupt_row_during_the_carried_read_is_taken(self):
        r = self.runner([init_msg(), "PAUSE", result()])
        _, b = self.carried(r)
        stop = r.enqueue(Item("system", "interrupt", "interrupt asked from the console",
                              sender="Priya"))
        self.assertTrue(_wait(lambda: self.outcome_of(r, stop) is not None, 3.0))
        self.assertEqual(self.outcome_of(r, stop), "delivered")
        self.assertGreaterEqual(self.clients[0].interrupts, 1)
        self.assertTrue(_wait(lambda: self.outcome_of(r, b) == "delivered", 10.0))

    def test_a_row_carried_past_an_interrupted_result_is_not_waited_on_forever(self):
        # the interrupt ended the CLI turn, and the CLI dropped the prompt
        # queued behind it: the carried read is bounded, the row goes back
        r = self.runner([init_msg(), ("SLOW", 30), result()])
        r.start()
        a = r.enqueue(self.op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        b = r.enqueue(self.op("second, folded"))
        self.assertTrue(_wait(lambda: len(self.clients[0].queries) == 2))
        t = time.monotonic()
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: self.outcome_of(r, a) == "delivered", 3.0))
        self.assertTrue(_wait(lambda: self.outcome_of(r, b) == "delivered", 10.0))
        self.assertLess(time.monotonic() - t, 10.0)

    def test_a_carried_row_the_cli_takes_up_is_still_closed_by_its_result(self):
        # the ordinary carried read is untouched: the echo comes, the row is
        # closed by the next result, nothing is requeued
        def factory(options):
            client = ScriptedClient(options, [[init_msg(), "PAUSE", result(num_turns=1)],
                                              [result(num_turns=2)]])
            self.clients.append(client)
            return client
        r = SdkRunner(self.home, client_factory=factory, drain_timeout_s=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(self.op("first"))
        self.assertTrue(_wait(lambda: self.clients and self.clients[0].paused))
        b = r.enqueue(self.op("second"))
        self.assertTrue(_wait(lambda: len(self.clients[0].queries) == 2))
        self.clients[0].resume()
        self.assertTrue(_wait(lambda: len(_results(r)) == 2))
        self.assertEqual([x["inbox_ids"] for x in _results(r)], [[a.inbox_id], [b.inbox_id]])
        self.assertEqual(len(self.clients), 1)             # no reconnect


if __name__ == "__main__":
    unittest.main()
