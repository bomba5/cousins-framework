"""#66: the carried read (a row written into the live turn whose echo had not
come when the CLI's result did) honours stop() and an interrupt, even when
the CLI never takes the row up."""
import asyncio
import json
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
from tests.runner.test_sdk import (ScriptedClient, _compile, _errors, _results, assistant, echo,
                                   init_msg, result)


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


class LateClient(DroppingClient):
    """A CLI that holds its `drop`-th message while idle and takes it up
    `lag` seconds after the first interrupt reaches it: the echo, then
    `late_turn` (the interrupt itself, sent to an idle CLI, cuts nothing).
    A client disconnected by then takes nothing."""

    def __init__(self, options, scripts, drop, lag, late_turn):
        super().__init__(options, scripts, drop)
        self.lag, self.late_turn = lag, late_turn
        self.armed = self.took = False

    async def interrupt(self):
        await super().interrupt()
        if not self.armed and len(self.queries) >= self.drop:
            self.armed = True
            asyncio.get_running_loop().call_later(self.lag, self._take)

    def _take(self):
        if not self.connected:
            return
        turn = _compile(self.late_turn)
        for el in turn:
            if getattr(el, "kind", None) in ("SLOW", "WAIT_FOR_INTERRUPT"):
                el.baseline = self.interrupts      # the interrupt so far did not cut it
        self.stream.extend([echo(self.queries[self.drop - 1]), *turn])
        self.took = True


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

    def runner(self, first_turn, late=None, **kw):
        """The first client drops the second message (the fold), or, given
        `late` (lag, turn), takes it up that long after an interrupt; any
        client after it (a reconnect) is an ordinary scripted one."""
        def factory(options):
            if not self.clients and late is not None:
                client = LateClient(options, [first_turn], drop=2, lag=late[0],
                                    late_turn=late[1])
            elif not self.clients:
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
        # an interrupt the user asked for is not a failure (review round 2, 7):
        # no errored state, no error event, no failure count, and the result
        # that sends b back cut nothing
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertNotIn("errored", states)
        self.assertEqual(_errors(r), [])
        back = [x for x in _results(r) if x.get("requeued") == [b.inbox_id]]
        self.assertEqual([(x["is_error"], x["interrupted"]) for x in back], [(False, False)])
        # the failure count is set after the idle move and the review gate:
        # read once the loop has stopped, when nothing can still set it (#102)
        r.stop(timeout=5)
        self.assertEqual(r._failures, 0)

    def test_a_late_echo_after_the_bound_never_runs_the_row_twice(self):
        # review round 2, 1: the CLI takes b 1.5 s after the interrupt, past
        # the 1.0 s bound. The client that holds b is gone before b goes back
        # to the queue: b reaches the old CLI once, and runs once more, on
        # the new client, as a turn of its own
        r = self.runner([init_msg(), "PAUSE", result()],
                        late=(1.5, [assistant(text="late"), result()]))
        _, b = self.carried(r)
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: self.outcome_of(r, b) == "delivered", 10.0))
        time.sleep(2.0)                                  # past the late echo
        said = lambda c: sum("second, folded" in json.dumps(q) for q in c.queries)
        self.assertEqual(said(self.clients[0]), 1, "b was written to the old CLI again")
        self.assertEqual(len(self.clients), 2, "the CLI holding b was never replaced")
        self.assertFalse(self.clients[0].took)
        self.assertEqual(sum(b.inbox_id in x["inbox_ids"] for x in _results(r)), 1)

    def test_stop_gives_the_echo_its_interrupt_brings_a_grace(self):
        # review round 2, 2: stop()'s interrupt is what makes the CLI take b,
        # 0.3 s later: b was consumed, so it is closed, never requeued
        r = self.runner([init_msg(), "PAUSE", result()],
                        late=(0.3, [assistant(text="late"), result()]))
        _, b = self.carried(r)
        t = time.monotonic()
        r.stop(timeout=10)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(self.outcome_of(r, b), "delivered")
        self.assertEqual(len(self.clients), 1)

    def test_an_interrupt_the_idle_cli_ignored_is_sent_again_to_the_carried_turn(self):
        # review round 2, 3: the interrupt reached the CLI while it was idle
        # and cut nothing; the CLI then takes b and would run it in full. The
        # echo sends the interrupt again, to the turn that is now live.
        r = self.runner([init_msg(), "PAUSE", result()],
                        late=(0.3, [("SLOW", 30), assistant(text="late"), result()]))
        _, b = self.carried(r)
        t = time.monotonic()
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: self.outcome_of(r, b) is not None, 5.0),
                        "the carried turn ran on, uninterrupted")
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertEqual(self.outcome_of(r, b), "delivered")
        self.assertGreaterEqual(self.clients[0].interrupts, 2)
        closing = [x for x in _results(r) if b.inbox_id in x["inbox_ids"]]
        self.assertTrue(closing[0]["interrupted"])
        self.assertEqual(len(self.clients), 1)

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
