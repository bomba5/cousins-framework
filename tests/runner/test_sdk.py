"""SdkRunner against a scripted client: the whole loop, no model."""
import asyncio
import time
import unittest

try:
    from claude_agent_sdk import (AssistantMessage, ResultMessage, SystemMessage,
                                  TextBlock, ToolUseBlock, UserMessage, ToolResultBlock)
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def init_msg(source="none", model="claude-haiku-4-5-20251001", session="s-1"):
    return SystemMessage(subtype="init", data={"apiKeySource": source, "model": model,
                                               "session_id": session})


def assistant(text=None, tool=None):
    content = []
    if tool:
        content.append(ToolUseBlock(id="tu-1", name=tool, input={"command": "true"}))
    if text:
        content.append(TextBlock(text=text))
    return AssistantMessage(content=content, model="m")


def result(num_turns=1, cost=0.01, is_error=False, session="s-1"):
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5,
                         is_error=is_error, num_turns=num_turns, session_id=session,
                         total_cost_usd=cost)


class ScriptedClient:
    """Models the real client: ONE message stream for the client's life (the
    scripts concatenated in order), and `receive_response()` yields from it
    up to and including the next ResultMessage, then stops; the next call
    continues where the last one left off. A cancelled read consumes nothing
    (the pop happens after the delay). Markers:
      "HANG"               blocks until interrupt() is called, then the stream
                           continues (a turn gone silent)
      "WAIT_FOR_INTERRUPT" blocks until interrupted, then yields a result
      "END"                the CLI died: this and every later read ends with
                           no ResultMessage
    An empty stream answers an unscripted query with one result().
    `delay` seconds before every element lets a test poke mid-turn."""
    def __init__(self, options, scripts, delay=0.0):
        self.options = options
        self.stream = [msg for script in scripts for msg in script]
        self.delay = delay
        self.queries = []
        self.interrupts = 0
        self.connected = False
        self.ended = False

    async def connect(self, prompt=None):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, prompt, session_id="default"):
        # The real client's contract: a str, or an async iterable of message
        # dicts (a bare dict would reach `async for` there and raise).
        if isinstance(prompt, str):
            self.queries.append(prompt)
            return
        async for message in prompt:
            self.queries.append(message)

    async def interrupt(self):
        self.interrupts += 1

    async def receive_response(self):
        while not self.ended:
            if not self.stream:
                self.stream.append(result())
            if self.delay:
                await asyncio.sleep(self.delay)
            msg = self.stream.pop(0)
            if isinstance(msg, str):
                if msg == "END":
                    self.ended = True
                    return
                baseline = self.interrupts if msg == "HANG" else 0
                while self.interrupts <= baseline:
                    await asyncio.sleep(0.02)
                if msg == "HANG":
                    continue
                msg = result(is_error=False)  # WAIT_FOR_INTERRUPT
            yield msg
            if isinstance(msg, ResultMessage):
                return


def _wait(pred, timeout=5.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestSdkRunner(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def _runner(self, scripts, **kw):
        made = {}
        delay = kw.pop("delay", 0.0)  # the client's knob, never SdkRunner's

        def factory(options):
            made["client"] = ScriptedClient(options, scripts, delay=delay)
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r, made

    def test_options_carry_the_key_only_when_given(self):
        r, _ = self._runner([])
        self.assertNotIn("ANTHROPIC_API_KEY", r.options().env)
        r2 = SdkRunner(self.home, client_factory=lambda o: ScriptedClient(o, []), api_key="sk-test")
        self.assertEqual(r2.options().env["ANTHROPIC_API_KEY"], "sk-test")

    def test_a_turn_records_init_text_tool_and_result_and_closes_the_row(self):
        r, made = self._runner([[init_msg("none"), assistant(tool="Bash"),
                                  assistant(text="done"), result()]])
        r.start()
        receipt = r.enqueue(Item("operator:priya", "chat", "hello", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        kinds = [e["kind"] for e in r.events()]
        for k in ("session_init", "turn_start", "tool", "text", "result"):
            self.assertIn(k, kinds)
        init = next(e for e in r.events() if e["kind"] == "session_init")
        self.assertEqual(init["payload"]["apiKeySource"], "none")
        self.assertEqual(r.inbox.get(receipt.inbox_id)["state"], "done")
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "delivered")
        prompt = made["client"].queries[0]
        self.assertEqual(prompt["message"]["content"][0]["text"].splitlines()[0][:16], "[operator:priya]")

    def test_a_midturn_operator_message_is_queried_into_the_live_turn(self):
        r, made = self._runner([[init_msg(), assistant(tool="Bash"), assistant(text="x"), result()]],
                               delay=0.15)
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        b = r.enqueue(Item("operator:priya", "chat", "second", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events()), timeout=5))
        self.assertEqual(len(made["client"].queries), 2, "the second was query()'d mid-turn")
        res = [e for e in r.events() if e["kind"] == "result"]
        self.assertEqual(len(res), 1)
        self.assertEqual(sorted(res[0]["payload"]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))

    def test_a_peer_message_waits_for_the_turn_boundary(self):
        r, made = self._runner([[init_msg(), assistant(tool="Bash"), result()],
                                [assistant(text="second turn"), result()]], delay=0.15)
        r.start()
        r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r.enqueue(Item("peer:testa", "chat", "peer", sender="Testa"))
        self.assertTrue(_wait(lambda: sum(e["kind"] == "result" for e in r.events()) == 2, timeout=6))
        self.assertEqual(len(made["client"].queries), 2)
        results = [e["payload"]["inbox_ids"] for e in r.events() if e["kind"] == "result"]
        self.assertEqual([len(x) for x in results], [1, 1])

    def test_interrupt_calls_the_client_and_marks_the_result(self):
        r, made = self._runner([[init_msg(), "WAIT_FOR_INTERRUPT"]])
        r.start()
        r.enqueue(Item("operator:priya", "chat", "slow", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=4))
        self.assertEqual(made["client"].interrupts, 1)
        res = [e for e in r.events() if e["kind"] == "result"][-1]
        self.assertTrue(res["payload"]["interrupted"])

    def test_a_client_exception_goes_to_errored_then_back_to_idle_and_fails_the_row(self):
        class Boom(ScriptedClient):
            async def receive_response(self):
                raise RuntimeError("transport died")
                yield  # pragma: no cover
        r = SdkRunner(self.home, client_factory=lambda o: Boom(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        receipt = r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "error" for e in r.events())))
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertIn("errored", states)
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "failed")

    def test_query_is_sent_as_an_async_iterable_not_a_dict(self):
        seen = []

        class Recording(ScriptedClient):
            async def query(self, prompt, session_id="default"):
                seen.append((type(prompt).__name__, hasattr(prompt, "__aiter__")))
                await super().query(prompt, session_id)
        r = SdkRunner(self.home, client_factory=lambda o: Recording(
            o, [[init_msg(), assistant(text="ok"), result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "hello", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        self.assertTrue(seen)
        for name, aiter in seen:
            self.assertNotEqual(name, "dict", "a bare dict breaks the real client")
            self.assertTrue(aiter, "query() takes a str or an async iterable")

    def test_a_hung_stream_times_out_and_fails_the_row(self):
        # "HANG": silent until interrupted, as a real CLI is (the drain's interrupt)
        r = SdkRunner(self.home, client_factory=lambda o: ScriptedClient(o, [[init_msg(), "HANG"]]),
                      turn_timeout_s=0.3)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        receipt = r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: any("exceeded" in e["payload"].get("error", "")
                                          for e in r.events() if e["kind"] == "error"),
                              timeout=2.0))
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=2.0))
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "failed")

    def test_a_stream_that_ends_without_a_result_fails_the_turn(self):
        r, made = self._runner([[init_msg(), assistant(text="partial"), "END"]])
        r.start()
        receipt = r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: any("without a result" in e["payload"].get("error", "")
                                          for e in r.events() if e["kind"] == "error")))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertIn("errored", [e["payload"]["to"] for e in r.events() if e["kind"] == "state"])
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "failed")
        res = [e for e in r.events() if e["kind"] == "result"]
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["payload"]["is_error"])

    def test_a_timed_out_turn_is_drained_so_the_next_turn_is_in_sync(self):
        clients = []

        def factory(options):
            clients.append(ScriptedClient(options, [
                [init_msg(), assistant(tool="Bash"), "HANG", result(num_turns=1, cost=0.01)],
                [assistant(text="second"), result(num_turns=7, cost=0.07)]]))
            return clients[-1]
        r = SdkRunner(self.home, client_factory=factory, turn_timeout_s=0.3)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: any("exceeded" in e["payload"].get("error", "")
                                          for e in r.events() if e["kind"] == "error"), timeout=2.0))
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=2.0))
        b = r.enqueue(Item("operator:priya", "chat", "second", sender="Priya"))
        self.assertTrue(_wait(lambda: sum(e["kind"] == "result" for e in r.events()) == 2))
        second = [e for e in r.events() if e["kind"] == "result"][1]["payload"]
        self.assertEqual(second["inbox_ids"], [b.inbox_id])
        self.assertEqual((second["is_error"], second["num_turns"], second["total_cost_usd"]),
                         (False, 7, 0.07))
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "failed")
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")
        self.assertEqual(len(clients), 1, "a drained stream needs no reconnect")
        self.assertEqual(clients[0].interrupts, 1)

    def test_a_failed_drain_reconnects_and_resumes(self):
        clients = []

        def factory(options):
            if not clients:
                scripts = [[init_msg(session="s-orig"), "HANG", "HANG"]]
            else:
                scripts = [[init_msg(session="s-orig"), assistant(text="back"),
                            result(num_turns=3, session="s-orig")]]
            clients.append(ScriptedClient(options, scripts))
            return clients[-1]
        r = SdkRunner(self.home, client_factory=factory, turn_timeout_s=0.3,
                      drain_timeout_s=0.3)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["payload"].get("error", "").startswith("reconnected:")
                                          for e in r.events() if e["kind"] == "error"), timeout=3.0))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertEqual(len(clients), 2)
        self.assertIsNone(clients[0].options.resume)
        self.assertEqual(clients[1].options.resume, "s-orig")
        self.assertFalse(clients[0].connected)
        reconnect = next(e for e in r.events() if e["kind"] == "error"
                         and e["payload"]["error"].startswith("reconnected:"))
        self.assertEqual(reconnect["payload"]["resumed"], "s-orig")
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "failed")
        b = r.enqueue(Item("operator:priya", "chat", "again", sender="Priya"))
        self.assertTrue(_wait(lambda: sum(e["kind"] == "result" for e in r.events()) == 2))
        second = [e for e in r.events() if e["kind"] == "result"][1]["payload"]
        self.assertEqual((second["inbox_ids"], second["is_error"], second["num_turns"]),
                         ([b.inbox_id], False, 3))
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")

    def test_a_stale_interrupt_does_not_kill_the_next_turn(self):
        r, made = self._runner([[init_msg(), result()],
                                [assistant(text="slow"), assistant(text="more"), result()]],
                               delay=0.15)
        r.start()
        r.enqueue(Item("operator:priya", "chat", "one", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        stale = r._turn_seq
        b = r.enqueue(Item("operator:priya", "chat", "two", sender="Priya"))
        self.assertTrue(_wait(lambda: r._turn_seq == stale + 1 and r.state() == "running"))
        # what a late interrupt() queued during turn 1 does when it finally runs
        asyncio.run_coroutine_threadsafe(r._interrupt_turn(stale), r._loop).result(timeout=2)
        self.assertTrue(_wait(lambda: sum(e["kind"] == "result" for e in r.events()) == 2))
        self.assertEqual(made["client"].interrupts, 0)
        last = [e for e in r.events() if e["kind"] == "result"][-1]["payload"]
        self.assertEqual(last["inbox_ids"], [b.inbox_id])
        self.assertFalse(last["interrupted"])
        self.assertTrue(any(e["kind"] == "system"
                            and e["payload"].get("subtype") == "interrupt_dropped"
                            for e in r.events()))

    def test_module_imports_without_the_sdk_installed(self):
        import importlib, sys
        saved = {k: v for k, v in sys.modules.items() if k.startswith("claude_agent_sdk")}
        for k in saved: sys.modules[k] = None
        try:
            import cousin_lib.runner.sdk as m
            importlib.reload(m)
        finally:
            for k, v in saved.items(): sys.modules[k] = v
            importlib.reload(m)


if __name__ == "__main__":
    unittest.main()
