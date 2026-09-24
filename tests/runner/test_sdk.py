"""SdkRunner against a scripted client: the whole loop, no model."""
import asyncio
import os
import time
import unittest

try:
    from claude_agent_sdk import (AssistantMessage, CLIConnectionError, HookEventMessage,
                                  ResultMessage, SystemMessage, TextBlock, ThinkingBlock,
                                  ToolResultBlock, ToolUseBlock, UserMessage)
except ImportError:  # the `sdk` extra is optional; discovery skips, never errors
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner import hooks, wake
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def init_msg(source="none", model="claude-haiku-4-5-20251001", session="s-1",
             tools=None, mcp_servers=None):
    data = {"apiKeySource": source, "model": model, "session_id": session}
    if tools is not None:
        data["tools"] = tools
    if mcp_servers is not None:
        data["mcp_servers"] = mcp_servers
    return SystemMessage(subtype="init", data=data)


def assistant(text=None, tool=None):
    content = []
    if tool:
        content.append(ToolUseBlock(id="tu-1", name=tool, input={"command": "true"}))
    if text:
        content.append(TextBlock(text=text))
    return AssistantMessage(content=content, model="m")


def result(num_turns=1, cost=0.01, is_error=False, session="s-1", usage=None):
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5,
                         is_error=is_error, num_turns=num_turns, session_id=session,
                         total_cost_usd=cost, usage=usage)


def asked_resume(options):
    """The session id these options resume, by either path: the store's
    (`resume`, the key lane) or the CLI's own flag (the login lane)."""
    return options.resume or (options.extra_args or {}).get("resume")


def echo(message):
    """What the CLI replays for a user message it consumed, with
    `--replay-user-messages`, as the SDK's parser builds it
    (claude_agent_sdk/_internal/message_parser.py, case "user"): a list
    content keeps its text blocks as TextBlocks and drops any block type
    it has no case for (an image); a str content stays a str."""
    content = message["message"]["content"] if isinstance(message, dict) else message
    if isinstance(content, str):
        return UserMessage(content=content)
    return UserMessage(content=[TextBlock(text=b["text"]) for b in content
                                if b.get("type") == "text"])


MARKERS = ("HANG", "WAIT_FOR_INTERRUPT", "END", "PAUSE")


class _Marker:
    def __init__(self, kind, seconds=0.0):
        self.kind, self.seconds = kind, seconds
        self.baseline = None   # interrupts seen before this marker could be released
        self.since = None      # when the reader first reached it

    def __repr__(self):
        return "<%s>" % self.kind


def _compile(turn):
    out = []
    for el in turn:
        if isinstance(el, str) and el in MARKERS:
            out.append(_Marker(el))
        elif isinstance(el, tuple) and el and el[0] == "SLOW":
            out.append(_Marker("SLOW", float(el[1])))
        elif isinstance(el, tuple) and el and el[0] == "CALL":
            marker = _Marker("CALL"); marker.fn = el[1]   # the model calling an in-process tool
            out.append(marker)
        else:
            out.append(el)
    return out


class ScriptedClient:
    """Models the real client: ONE message stream for the client's life,
    and `receive_response()` yields from it up to and including the next
    ResultMessage, then stops; the next call continues where the last one
    left off.

    Each script is one CLI turn. A query() while the CLI is idle (the
    stream empty, or its head a ResultMessage: the model has finished)
    starts a NEW CLI turn after whatever is still unread: its echo, then
    the next script, or a default text and result when the scripts are
    used up. A query() while a turn is still generating (anything but a
    result before the next ResultMessage, PAUSE aside) is folded into it:
    its echo is inserted before the remaining elements. A query() on a
    client that is not connected, or whose CLI died, raises
    CLIConnectionError before anything is written, as the real one does.

    Markers (every one is peeked, waited on, then popped, so a cancelled
    read consumes nothing):
      "HANG"               silent until interrupt() is called after the
                           reader reached it, then the stream continues
      "WAIT_FOR_INTERRUPT" silent until the turn is interrupted, then yields
                           a result
      ("SLOW", s)          silent for s seconds or until the turn is
                           interrupted, then the stream continues
      "PAUSE"              the transport holds the stream here until
                           resume(); the model is NOT generating, so a
                           query() meanwhile starts a new CLI turn when only
                           a result follows
      "END"                the CLI died: this and every later read ends with
                           no ResultMessage
      ("CALL", fn)         calls fn() when the reader reaches it (a tool the
                           model calls), then the stream continues
    Anything inserted ahead of a marker being waited on is yielded first.
    `delay` seconds before every element lets a test poke mid-turn."""

    def __init__(self, options, scripts, delay=0.0):
        self.options = options
        self.turns = [_compile(script) for script in scripts]
        self.stream = []
        self.delay = delay
        self.queries = []
        self.interrupts = 0
        self.connected = False
        self.ended = False
        self.paused = False
        self._go = False

    async def connect(self, prompt=None):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def resume(self):
        self._go = True

    def _generating(self):
        for el in self.stream:
            if isinstance(el, ResultMessage):
                return False
            if isinstance(el, _Marker) and el.kind == "PAUSE":
                continue
            return True
        return False

    async def query(self, prompt, session_id="default"):
        if not self.connected or self.ended:
            raise CLIConnectionError("Not connected. Call connect() first.")
        # The real client's contract: a str, or an async iterable of message
        # dicts (a bare dict would reach `async for` there and raise).
        messages = [prompt] if isinstance(prompt, str) else [m async for m in prompt]
        for message in messages:
            self.queries.append(message)
            if self._generating():
                self.stream.insert(0, echo(message))
                continue
            turn = self.turns.pop(0) if self.turns else [assistant(text="ok"), result()]
            for el in turn:
                if isinstance(el, _Marker) and el.kind in ("WAIT_FOR_INTERRUPT", "SLOW"):
                    el.baseline = self.interrupts
            self.stream.extend([echo(message), *turn])

    async def interrupt(self):
        if not self.connected or self.ended:
            raise CLIConnectionError("Not connected. Call connect() first.")
        self.interrupts += 1

    def _released(self, marker):
        if marker.kind == "PAUSE":
            if self._go:
                self._go, self.paused = False, False
                return True
            self.paused = True
            return False
        if self.interrupts > marker.baseline:
            return True
        return (marker.kind == "SLOW"
                and time.monotonic() - marker.since >= marker.seconds)

    def _remove(self, marker):
        for i, el in enumerate(self.stream):
            if el is marker:
                del self.stream[i]
                return

    async def receive_response(self):
        while not self.ended:
            if not self.stream:          # an idle CLI says nothing
                await asyncio.sleep(0.02)
                continue
            if self.delay:
                await asyncio.sleep(self.delay)
            head = self.stream[0]
            if isinstance(head, _Marker):
                if head.kind == "CALL":
                    self._remove(head)
                    head.fn()
                    continue
                if head.kind == "END":
                    self.ended = True
                    return
                if head.baseline is None:
                    head.baseline = self.interrupts
                if head.since is None:
                    head.since = time.monotonic()
                while self.stream and self.stream[0] is head and not self._released(head):
                    await asyncio.sleep(0.02)
                if not self.stream or self.stream[0] is not head:
                    continue             # something was inserted ahead of it
                self._remove(head)
                if head.kind != "WAIT_FOR_INTERRUPT":
                    continue
                msg = result(is_error=False)
            else:
                msg = self.stream.pop(0)
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


def _results(r, drained=False):
    return [e["payload"] for e in r.events() if e["kind"] == "result"
            and bool(e["payload"].get("drained")) == drained]


def _errors(r):
    return [e["payload"].get("error", "") for e in r.events() if e["kind"] == "error"]


class TestScriptedClient(HermeticCase):
    def test_a_cancelled_read_leaves_a_hang_in_the_stream(self):
        async def go():
            c = ScriptedClient(None, [["HANG", assistant(text="after"), result()]])
            await c.connect()
            await c.query("x")
            it = c.receive_response().__aiter__()
            first = await it.__anext__()                 # the echo
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(it.__anext__(), 0.1)   # cancelled on the HANG
            await c.interrupt()
            it2 = c.receive_response().__aiter__()
            second = await asyncio.wait_for(it2.__anext__(), 1.0)
            await it2.aclose()
            return first, second
        first, second = asyncio.run(go())
        self.assertIsInstance(first, UserMessage)
        self.assertEqual(second.content[0].text, "after")

    def test_a_query_while_generating_is_echoed_ahead_of_the_rest(self):
        async def go():
            c = ScriptedClient(None, [[assistant(text="a"), assistant(text="b"), result()]])
            await c.connect()
            await c.query("one")
            await c.query("two")
            return [m async for m in c.receive_response()]
        msgs = asyncio.run(go())
        self.assertEqual([m.content for m in msgs[:2]], ["two", "one"])
        self.assertIsInstance(msgs[-1], ResultMessage)

    def test_a_query_when_only_the_result_is_left_starts_a_new_cli_turn(self):
        async def go():
            c = ScriptedClient(None, [[assistant(text="a"), result(num_turns=1)]])
            await c.connect()
            await c.query("one")
            it = c.receive_response()
            got = [await it.__anext__(), await it.__anext__()]   # echo, text
            await c.query("two")                                   # only the result is left
            got += [m async for m in it]
            got += [m async for m in c.receive_response()]
            return got
        msgs = asyncio.run(go())
        kinds = [type(m).__name__ for m in msgs]
        self.assertEqual(kinds, ["UserMessage", "AssistantMessage", "ResultMessage",
                                 "UserMessage", "AssistantMessage", "ResultMessage"])
        self.assertEqual(msgs[3].content, "two")

    def test_an_unconnected_client_refuses_a_query(self):
        async def go():
            c = ScriptedClient(None, [])
            await c.query("x")
        with self.assertRaises(CLIConnectionError):
            asyncio.run(go())


class TestSdkRunner(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def _runner(self, scripts, **kw):
        made = {"clients": []}
        delay = kw.pop("delay", 0.0)  # the client's knob, never SdkRunner's

        def factory(options):
            made["client"] = ScriptedClient(options, scripts, delay=delay)
            made["clients"].append(made["client"])
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory, **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r, made

    def _op(self, body):
        return Item("operator:priya", "chat", body, sender="Priya")

    # -- options -------------------------------------------------------------
    def test_options_carry_the_key_only_when_given(self):
        r, _ = self._runner([])
        self.assertNotIn("ANTHROPIC_API_KEY", r.options().env)
        r2 = SdkRunner(self.home, client_factory=lambda o: ScriptedClient(o, []), api_key="sk-test")
        self.assertEqual(r2.options().env["ANTHROPIC_API_KEY"], "sk-test")

    def test_the_cousin_tools_are_always_loaded_never_deferred(self):
        # The CLI defers MCP tools behind its tool search; a cousin then
        # has to search before its first memory, send or reply call.
        server = self._runner([])[0].options().mcp_servers["cousin"]
        self.assertEqual(server["type"], "sdk")
        self.assertIs(server["alwaysLoad"], True)

    def test_options_ask_the_cli_to_replay_every_user_message(self):
        r, _ = self._runner([])
        self.assertIn("replay-user-messages", r.options().extra_args)
        self.assertIsNone(r.options().extra_args["replay-user-messages"])

    # -- one turn ----------------------------------------------------------------
    def test_a_turn_records_init_text_tool_and_result_and_closes_the_row(self):
        r, made = self._runner([[init_msg("none"), assistant(tool="Bash"),
                                  assistant(text="done"), result()]])
        r.start()
        receipt = r.enqueue(self._op("hello"))
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

    def test_session_init_records_the_tools_and_mcp_servers_the_cli_offers(self):
        r, _ = self._runner([[init_msg(tools=["mcp__cousin__memory", "mcp__cousin__reply"],
                                       mcp_servers=[{"name": "cousin", "status": "connected"}]),
                              assistant(text="ok"), result()]])
        r.start()
        r.enqueue(self._op("hello"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        init = next(e for e in r.events() if e["kind"] == "session_init")
        self.assertEqual(init["payload"]["tools"], ["mcp__cousin__memory", "mcp__cousin__reply"])
        self.assertEqual(init["payload"]["mcp_servers"], [{"name": "cousin", "status": "connected"}])

    def test_session_init_defaults_tools_and_mcp_servers_to_empty_lists(self):
        r, _ = self._runner([[init_msg(), assistant(text="ok"), result()]])
        r.start()
        r.enqueue(self._op("hello"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        init = next(e for e in r.events() if e["kind"] == "session_init")
        self.assertEqual(init["payload"]["tools"], [])
        self.assertEqual(init["payload"]["mcp_servers"], [])

    # -- usage (A1) ----------------------------------------------------------
    def test_a_successful_results_usage_dict_lands_in_the_event(self):
        usage = {"input_tokens": 10, "output_tokens": 3,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 128}
        r, _ = self._runner([[init_msg(), assistant(text="ok"), result(usage=usage)]])
        r.start()
        r.enqueue(self._op("hello"))
        self.assertTrue(_wait(lambda: _results(r)))
        self.assertEqual(_results(r)[0]["usage"], usage)

    def test_a_result_with_no_usage_carries_none(self):
        r, _ = self._runner([[init_msg(), assistant(text="ok"), result()]])
        r.start()
        r.enqueue(self._op("hello"))
        self.assertTrue(_wait(lambda: _results(r)))
        self.assertIsNone(_results(r)[0]["usage"])

    def test_the_echo_of_the_row_is_recorded_as_a_user_event_naming_it(self):
        r, _ = self._runner([[init_msg(), assistant(text="done"), result()]])
        r.start()
        a = r.enqueue(self._op("hello"))
        self.assertTrue(_wait(lambda: _results(r)))
        users = [e["payload"] for e in r.events() if e["kind"] == "user"]
        self.assertEqual([u["echo_of"] for u in users], [a.inbox_id])
        self.assertIn("hello", users[0]["text"])

    def test_a_result_before_the_rows_echo_does_not_close_it(self):
        # the stream still holds a stale result from an earlier CLI turn
        made = {}

        def factory(options):
            made["client"] = ScriptedClient(options, [[init_msg(), assistant(text="ok"),
                                                       result(num_turns=5)]])
            made["client"].stream.append(result(num_turns=9))
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: len(_results(r)) == 2))
        res = _results(r)
        self.assertEqual([(x["inbox_ids"], x["num_turns"]) for x in res],
                         [([], 9), ([a.inbox_id], 5)])
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "delivered")

    # -- folding (A1) ------------------------------------------------------------
    def test_a_midturn_operator_message_is_queried_into_the_live_turn(self):
        # PAUSE holds the stream open until the second row is in: a timed
        # delay left the fold to the scheduler and flaked under load
        r, made = self._runner([[init_msg(), assistant(tool="Bash"), "PAUSE",
                                 assistant(text="x"), result()]])
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        b = r.enqueue(self._op("second"))
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 2, timeout=5),
                        "the second was query()'d mid-turn")
        made["client"].resume()
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events()), timeout=5))
        res = _results(r)
        self.assertEqual(len(res), 1)
        self.assertEqual(sorted(res[0]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))

    def test_a_fold_that_lands_mid_stream_is_echoed_and_closed_by_the_first_result(self):
        r, made = self._runner([[init_msg(), "PAUSE", assistant(text="answer"), result()]])
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        b = r.enqueue(self._op("second"))
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 2))
        made["client"].resume()
        self.assertTrue(_wait(lambda: r.state() == "idle" and _results(r)))
        time.sleep(0.3)
        res = _results(r)
        self.assertEqual(len(res), 1)
        self.assertEqual(sorted(res[0]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))
        for i in (a, b):
            self.assertEqual(r.inbox.get(i.inbox_id)["outcome"], "delivered")

    def test_a_fold_after_the_last_message_is_closed_by_its_own_result_and_nothing_shifts(self):
        r, made = self._runner([[init_msg(), assistant(text="one"), "PAUSE", result(num_turns=1)],
                                [assistant(text="two"), result(num_turns=2)],
                                [assistant(text="three"), result(num_turns=3)]])
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        b = r.enqueue(self._op("second, while only the result is left"))
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 2))
        made["client"].resume()
        self.assertTrue(_wait(lambda: len(_results(r)) == 2))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        c = r.enqueue(self._op("third"))
        self.assertTrue(_wait(lambda: len(_results(r)) == 3))
        res = _results(r)
        self.assertEqual([(x["inbox_ids"], x["num_turns"]) for x in res],
                         [([a.inbox_id], 1), ([b.inbox_id], 2), ([c.inbox_id], 3)])
        kinds = [(e["kind"], e["payload"].get("echo_of")) for e in r.events()
                 if e["kind"] in ("user", "result")]
        self.assertEqual(kinds[:4], [("user", a.inbox_id), ("result", None),
                                     ("user", b.inbox_id), ("result", None)])
        for i in (a, b, c):
            self.assertEqual(r.inbox.get(i.inbox_id)["outcome"], "delivered")

    def test_nothing_is_folded_once_an_interrupt_is_requested(self):
        r, made = self._runner([[init_msg(), ("SLOW", 30), "PAUSE",
                                 assistant(text="winding down"), result()]])
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: made["client"].paused))
        b = r.enqueue(self._op("after the interrupt"))
        time.sleep(0.6)   # three poll periods
        self.assertEqual(len(made["client"].queries), 1)
        made["client"].resume()
        self.assertTrue(_wait(lambda: len(_results(r)) == 2))
        res = _results(r)
        self.assertEqual((res[0]["inbox_ids"], res[0]["interrupted"]), ([a.inbox_id], True))
        self.assertEqual((res[1]["inbox_ids"], res[1]["interrupted"]), ([b.inbox_id], False))

    def test_a_midturn_peer_message_is_queried_into_the_live_turn(self):
        """#118: a peer (a cousin, thread peer:<slug>) folds like an
        operator: a coordinator's STOP must reach a peer in a long turn.
        It goes through the same write as an operator fold (`_send`), with
        its thread in the envelope header."""
        r, made = self._runner([[init_msg(), assistant(tool="Bash"), "PAUSE",
                                 assistant(text="x"), result()]])
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        b = r.enqueue(Item("peer:testa", "chat", "STOP", sender="Testa"))
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 2, timeout=5),
                        "the peer row was query()'d mid-turn")
        text = made["client"].queries[1]["message"]["content"][0]["text"]
        self.assertTrue(text.startswith("[peer:testa] chat from Testa"), text)
        self.assertIn("STOP", text)
        made["client"].resume()
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events()), timeout=5))
        res = _results(r)
        self.assertEqual(len(res), 1)
        self.assertEqual(sorted(res[0]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")

    def test_meeting_loop_and_schedule_rows_wait_for_the_turn_boundary(self):
        """What stays unfolded (#118 kept it): a meeting line is a round's
        turn, a loop or a schedule is the cousin's own timer; each is a
        turn of its own, never written into someone else's."""
        r, made = self._runner([[init_msg(), assistant(tool="Bash"), "PAUSE",
                                 assistant(text="x"), result()]])
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        # each its own turn, in priority order: meeting, schedule, loop
        waiting = [r.enqueue(Item("meeting:7", "meeting", "your turn", sender="")),
                   r.enqueue(Item("schedule", "schedule", "timer", sender="")),
                   r.enqueue(Item("loop:heartbeat", "loop", "beat", sender=""))]
        time.sleep(4 * r.poll_s + 0.2)   # several folds had their chance
        self.assertEqual(len(made["client"].queries), 1)
        made["client"].resume()
        self.assertTrue(_wait(lambda: len(_results(r)) == 4, timeout=8))
        self.assertEqual([x["inbox_ids"] for x in _results(r)],
                         [[a.inbox_id]] + [[w.inbox_id] for w in waiting])

    def test_with_a_peer_folded_reply_never_targets_the_peer_and_send_does(self):
        """#118: an operator turn with a peer folded has two live threads.
        reply is the chat surface only: named, the peer thread is refused
        with the send hint; unnamed, it goes to the one surface thread
        (the peer is no candidate). send reaches the peer."""
        from unittest import mock
        from cousin_lib.runner import tools
        from tests.runner.test_tools import _install
        _, self.home = _install(self)
        r, made = self._runner([[init_msg(), assistant(tool="Bash"), "PAUSE",
                                 assistant(text="x"), result()]])
        r.start()
        r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        r.enqueue(Item("peer:testa", "chat", "STOP", sender="Testa"))
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 2, timeout=5))
        self.assertEqual(r.turn.snapshot(), (True, ("operator:priya", "peer:testa")))
        ctx = r.tool_context
        text, err = tools.call(ctx, "reply", {"text": "stopping", "thread": "peer:testa"})
        self.assertTrue(err, text)
        self.assertIn("send", text)
        text, err = tools.call(ctx, "reply", {"text": "on it"})
        self.assertFalse(err, text)
        self.assertIn("replied to priya", text)
        with mock.patch("cousin_lib.chat.send_message", return_value={"ok": True}) as sm:
            text, err = tools.call(ctx, "send", {"to": "testa", "text": "stopping"})
        self.assertFalse(err, text)
        self.assertEqual(sm.call_args.args[2], "testa")
        made["client"].resume()
        self.assertTrue(_wait(lambda: len(_results(r)) == 1, timeout=5))

    # -- interrupt ---------------------------------------------------------------
    def test_interrupt_calls_the_client_and_marks_the_result(self):
        r, made = self._runner([[init_msg(), "WAIT_FOR_INTERRUPT"]])
        r.start()
        r.enqueue(self._op("slow"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=4))
        self.assertEqual(made["client"].interrupts, 1)
        self.assertTrue(_results(r)[-1]["interrupted"])

    # -- waiting_permission --------------------------------------------------------
    def _ask_permission(self, r):
        """What the PermissionRequest hook does mid-turn, under the runner's lock."""
        cbs = hooks.callbacks(r.home, slug="wren", root=r.home, machine=r.machine,
                              stream=r.stream, lock=r._lock)
        asyncio.run(cbs["PermissionRequest"]({"hook_event_name": "PermissionRequest",
                                              "tool_name": "Bash", "tool_input": {}}, None, {}))
        self.assertEqual(r.state(), "waiting_permission")

    def _reached_init(self, r):
        return _wait(lambda: any(e["kind"] == "session_init" for e in r.events()))

    def test_a_message_while_waiting_permission_moves_the_machine_back_to_running(self):
        hook_event = HookEventMessage(subtype="hook_response", data={},
                                      hook_event_name="PermissionRequest")
        r, made = self._runner([[init_msg(), "PAUSE", hook_event, "PAUSE",
                                 assistant(text="granted"), result()]])
        r.start()
        r.enqueue(self._op("needs a tool"))
        self.assertTrue(self._reached_init(r))
        self._ask_permission(r)
        made["client"].resume()
        # the hook's own event, not any `system` one: a start appends `fresh` (Task 11)
        self.assertTrue(_wait(lambda: any(e["kind"] == "system"
                                          and e["payload"].get("subtype") == "hook_response"
                                          for e in r.events())
                              and made["client"].paused))
        self.assertEqual(r.state(), "waiting_permission")   # a hook's own event settles nothing
        made["client"].resume()
        # The machine's state is set before its `state` event is appended, so
        # wait for the idle EVENT, not for state() == "idle" (seen flaky under load).
        def moves():
            return [(e["payload"]["from"], e["payload"]["to"])
                    for e in r.events() if e["kind"] == "state"]
        self.assertTrue(_wait(lambda: _results(r) and ("running", "idle") in moves()))
        self.assertIn(("waiting_permission", "running"), moves())
        self.assertEqual(moves()[-1], ("running", "idle"))

    def test_a_turn_that_times_out_while_waiting_permission_fails_and_recovers(self):
        r, _ = self._runner([[init_msg(), "HANG", result()]], idle_timeout_s=1.0)
        r.start()
        receipt = r.enqueue(self._op("x"))
        self.assertTrue(self._reached_init(r))
        self._ask_permission(r)
        self.assertTrue(_wait(lambda: any("no message for 1.0s" in e for e in _errors(r))))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "failed")
        moves = [(e["payload"]["from"], e["payload"]["to"]) for e in r.events() if e["kind"] == "state"]
        self.assertIn(("waiting_permission", "errored"), moves)

    def test_a_turn_waiting_permission_can_be_interrupted(self):
        r, made = self._runner([[init_msg(), "WAIT_FOR_INTERRUPT"]])
        r.start()
        r.enqueue(self._op("slow"))
        self.assertTrue(self._reached_init(r))
        self._ask_permission(r)
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=4))
        self.assertEqual(made["client"].interrupts, 1)
        self.assertTrue(_results(r)[-1]["interrupted"])

    def test_a_stale_interrupt_does_not_kill_the_next_turn(self):
        r, made = self._runner([[init_msg(), result()],
                                [assistant(text="slow"), assistant(text="more"), result()]],
                               delay=0.15)
        r.start()
        r.enqueue(self._op("one"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        stale = r._turn_seq
        b = r.enqueue(self._op("two"))
        self.assertTrue(_wait(lambda: r._turn_seq == stale + 1 and r.state() == "running"))
        # what a late interrupt() queued during turn 1 does when it finally runs
        asyncio.run_coroutine_threadsafe(r._interrupt_turn(stale), r._loop).result(timeout=2)
        self.assertTrue(_wait(lambda: len(_results(r)) == 2))
        self.assertEqual(made["client"].interrupts, 0)
        last = _results(r)[-1]
        self.assertEqual(last["inbox_ids"], [b.inbox_id])
        self.assertFalse(last["interrupted"])
        self.assertTrue(any(e["kind"] == "system"
                            and e["payload"].get("subtype") == "interrupt_dropped"
                            for e in r.events()))

    def test_an_interrupt_after_the_result_never_reaches_the_cli(self):
        # turn 1's result is read; the carried row's CLI turn has not shown
        # its echo yet (delay), so the CLI is between turns: not live
        r, made = self._runner([[init_msg(), assistant(text="one"), "PAUSE", result()],
                                [assistant(text="two"), result()]], delay=0.6)
        r.start()
        r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused, timeout=8))
        r.enqueue(self._op("second"))
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 2))
        made["client"].resume()
        self.assertTrue(_wait(lambda: len(_results(r)) == 1, timeout=4))
        seq = r._turn_seq
        asyncio.run_coroutine_threadsafe(r._interrupt_turn(seq), r._loop).result(timeout=2)
        self.assertEqual(made["client"].interrupts, 0)
        self.assertTrue(_wait(lambda: len(_results(r)) == 2, timeout=6))
        self.assertFalse(_results(r)[1]["interrupted"])
        self.assertTrue(any(e["kind"] == "system"
                            and e["payload"].get("subtype") == "interrupt_dropped"
                            for e in r.events()))

    # -- failures ------------------------------------------------------------------
    def test_a_client_exception_goes_to_errored_then_back_to_idle_and_fails_the_row(self):
        class Boom(ScriptedClient):
            async def receive_response(self):
                raise RuntimeError("transport died")
                yield  # pragma: no cover
        r = SdkRunner(self.home, client_factory=lambda o: Boom(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        receipt = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "error" for e in r.events())))
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertIn("errored", states)
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "failed")
        self.assertTrue(_wait(lambda: _results(r)))
        self.assertIn("usage", _results(r)[0])
        self.assertIsNone(_results(r)[0]["usage"])

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
        r.enqueue(self._op("hello"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        self.assertTrue(seen)
        for name, aiter in seen:
            self.assertNotEqual(name, "dict", "a bare dict breaks the real client")
            self.assertTrue(aiter, "query() takes a str or an async iterable")

    def test_a_hung_stream_times_out_and_fails_the_row(self):
        # "HANG": silent until interrupted, as a real CLI is (the drain's
        # interrupt), which then ends the turn with a result
        r, _ = self._runner([[init_msg(), "HANG", result()]], idle_timeout_s=1.0)
        r.start()
        receipt = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: any("no message for 1.0s" in e for e in _errors(r)),
                              timeout=5.0))
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=5.0))
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "failed")

    def test_the_idle_timeout_bounds_the_gap_not_the_whole_turn(self):
        r, _ = self._runner([[init_msg(), assistant(text="a"), assistant(text="b"),
                              assistant(text="c"), assistant(text="d"), result()]],
                            delay=0.3, idle_timeout_s=1.0)
        r.start()
        a = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: _results(r), timeout=8))
        self.assertEqual(_errors(r), [])
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "delivered")

    def test_an_optional_whole_turn_ceiling_ends_a_turn_that_keeps_talking(self):
        r, _ = self._runner([[init_msg()] + [assistant(text=str(i)) for i in range(40)]
                             + [result()]], delay=0.1, idle_timeout_s=5.0, turn_timeout_s=1.0)
        r.start()
        a = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: any("turn exceeded 1.0s" in e for e in _errors(r)),
                              timeout=8))
        self.assertTrue(_wait(lambda: r.inbox.get(a.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "failed")

    def test_a_stream_that_ends_without_a_result_fails_the_turn(self):
        r, made = self._runner([[init_msg(), assistant(text="partial"), "END"]])
        r.start()
        receipt = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: any("without a result" in e for e in _errors(r))))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertIn("errored", [e["payload"]["to"] for e in r.events() if e["kind"] == "state"])
        self.assertEqual(r.inbox.get(receipt.inbox_id)["outcome"], "failed")
        res = _results(r)
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["is_error"])

    def test_a_timed_out_turn_is_drained_so_the_next_turn_is_in_sync(self):
        r, made = self._runner([
            [init_msg(), assistant(tool="Bash"), "HANG", result(num_turns=1, cost=0.01)],
            [assistant(text="second"), result(num_turns=7, cost=0.07)]], idle_timeout_s=1.0)
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: any("no message for" in e for e in _errors(r)), timeout=5.0))
        self.assertTrue(_wait(lambda: r.state() == "idle", timeout=5.0))
        b = r.enqueue(self._op("second"))
        self.assertTrue(_wait(lambda: len(_results(r)) == 2))
        second = _results(r)[1]
        self.assertEqual(second["inbox_ids"], [b.inbox_id])
        self.assertEqual((second["is_error"], second["num_turns"], second["total_cost_usd"]),
                         (False, 7, 0.07))
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "failed")
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")
        self.assertEqual(len(made["clients"]), 1, "a drained stream needs no reconnect")
        self.assertEqual(made["clients"][0].interrupts, 1)

    def test_the_drained_result_is_recorded_so_its_cost_is_not_lost(self):
        r, _ = self._runner([[init_msg(), "HANG", result(num_turns=4, cost=0.04)]],
                            idle_timeout_s=1.0)
        r.start()
        r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: _results(r, drained=True), timeout=6))
        drained = _results(r, drained=True)[0]
        self.assertEqual((drained["inbox_ids"], drained["num_turns"], drained["total_cost_usd"]),
                         ([], 4, 0.04))

    def test_the_drained_results_usage_is_recorded_too(self):
        usage = {"input_tokens": 5, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        r, _ = self._runner([[init_msg(), "HANG", result(num_turns=4, cost=0.04, usage=usage)]],
                            idle_timeout_s=1.0)
        r.start()
        r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: _results(r, drained=True), timeout=6))
        self.assertEqual(_results(r, drained=True)[0]["usage"], usage)

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
        r = SdkRunner(self.home, client_factory=factory, idle_timeout_s=1.0,
                      drain_timeout_s=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: any(e.startswith("reconnected:") for e in _errors(r)),
                              timeout=6.0))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertEqual(len(clients), 2)
        self.assertIsNone(asked_resume(clients[0].options))
        # the login lane (the init said "none"): the CLI's own --resume (Task 11)
        self.assertEqual(clients[1].options.extra_args["resume"], "s-orig")
        self.assertFalse(clients[0].connected)
        reconnect = next(e for e in r.events() if e["kind"] == "error"
                         and e["payload"]["error"].startswith("reconnected:"))
        self.assertEqual(reconnect["payload"]["resumed"], "s-orig")
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "failed")
        b = r.enqueue(self._op("again"))
        self.assertTrue(_wait(lambda: len(_results(r)) == 2))
        second = _results(r)[1]
        self.assertEqual((second["inbox_ids"], second["is_error"], second["num_turns"]),
                         ([b.inbox_id], False, 3))
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")

    def test_a_row_whose_query_wrote_nothing_is_requeued_not_failed(self):
        clients = []

        class DeadTransport(ScriptedClient):
            async def connect(self, prompt=None):
                self.connected = False   # "connected", but the transport is gone

        def factory(options):
            cls = DeadTransport if not clients else ScriptedClient
            clients.append(cls(options, [[init_msg(), assistant(text="ok"), result()]]))
            return clients[-1]
        r = SdkRunner(self.home, client_factory=factory, drain_timeout_s=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: r.inbox.get(a.inbox_id)["state"] == "done", timeout=6))
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "delivered")
        self.assertEqual(len(clients), 2)
        failed = [x for x in _results(r) if x["is_error"]]
        self.assertEqual([(x["inbox_ids"], x["requeued"]) for x in failed], [([], [a.inbox_id])])

    def test_consecutive_failed_turns_back_off_exponentially_and_a_success_resets(self):
        r, _ = self._runner([[result(is_error=True)] for _ in range(5)]
                            + [[assistant(text="fine"), result()]])
        r.backoff_base_s, r.backoff_cap_s = 0.1, 0.3
        for i in range(5):   # loop rows: never folded, so one turn each
            r.enqueue(Item("loop:heartbeat", "loop", "fails %d" % i, sender=""))
        r.start()
        self.assertTrue(_wait(lambda: len(_results(r)) == 5, timeout=8))
        ok = r.enqueue(self._op("works"))
        self.assertTrue(_wait(lambda: r.inbox.get(ok.inbox_id)["state"] == "done", timeout=8))
        backoffs = [e["payload"]["seconds"] for e in r.events()
                    if e["kind"] == "system" and e["payload"].get("subtype") == "backoff"]
        self.assertEqual(backoffs, [0.1, 0.2, 0.3])
        self.assertEqual(r.inbox.get(ok.inbox_id)["outcome"], "delivered")
        self.assertTrue(_wait(lambda: r._failures == 0, timeout=8))
        self.assertEqual(r._failures, 0)

    def test_a_connect_failure_at_start_ends_the_worker(self):
        class NoConnect(ScriptedClient):
            async def connect(self, prompt=None):
                raise RuntimeError("no cli")
        r = SdkRunner(self.home, client_factory=lambda o: NoConnect(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        self.assertEqual(r.state(), "errored")
        self.assertTrue(any("no cli" in e for e in _errors(r)))
        self.assertIn("no cli", r.fatal)

    def test_a_failed_reconnect_ends_the_worker_and_leaves_the_queue_alone(self):
        clients = []

        def factory(options):
            if clients:
                raise RuntimeError("cli gone for good")
            clients.append(ScriptedClient(options, [[init_msg(), "HANG", "HANG"]]))
            return clients[-1]
        r = SdkRunner(self.home, client_factory=factory, idle_timeout_s=1.0,
                      drain_timeout_s=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        b = r.enqueue(Item("loop:heartbeat", "loop", "waiting", sender=""))   # never folded
        self.assertTrue(_wait(lambda: not r.worker_alive(), timeout=8))
        self.assertTrue(any(e.startswith("reconnect failed:") for e in _errors(r)))
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "failed")
        self.assertEqual(r.inbox.get(b.inbox_id)["state"], "queued")
        self.assertEqual(r.state(), "errored")

    def test_no_new_client_is_started_after_stop(self):
        r, made = self._runner([[init_msg(), "HANG", "HANG", "HANG"]],
                               idle_timeout_s=1.0, drain_timeout_s=1.0)
        r.start()
        r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        t = time.monotonic()
        r.stop(timeout=5)
        self.assertLess(time.monotonic() - t, 4.0)
        r._thread.join(5)
        self.assertFalse(r._thread.is_alive())
        self.assertEqual(len(made["clients"]), 1)

    def test_a_failure_while_closing_rows_still_resyncs_the_stream(self):
        r, made = self._runner([[init_msg(), "HANG", result(num_turns=1)],
                                [assistant(text="next"), result(num_turns=2)]],
                               idle_timeout_s=1.0)
        real_done = r.inbox.done
        calls = {"n": 0}

        def flaky_done(inbox_id, outcome, detail=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_done(inbox_id, outcome, detail)
        r.inbox.done = flaky_done
        r.start()
        r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "system"
                                          and e["payload"].get("subtype") == "drained"
                                          for e in r.events()), timeout=6))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        b = r.enqueue(self._op("second"))
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")
        self.assertEqual([x["num_turns"] for x in _results(r) if not x["is_error"]], [2])

    # -- the doorbell (A4) -------------------------------------------------------
    def test_a_wake_socket_that_cannot_bind_falls_back_to_polling(self):
        run = self.home / "run"
        os.rmdir(run)
        run.write_text("not a directory")
        r, _ = self._runner([[init_msg(), assistant(text="ok"), result()]])
        r.start()
        a = r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: r.inbox.get(a.inbox_id)["state"] == "done"))
        self.assertTrue(any(str(wake.socket_path(self.home)) in e for e in _errors(r)))
        self.assertTrue(r.worker_alive())

    # -- the event floor (A5) ------------------------------------------------------
    def test_every_message_leaves_at_least_one_event(self):
        thinking = AssistantMessage(content=[ThinkingBlock(thinking="hmm", signature="s")],
                                    model="m")
        tool_list = UserMessage(content=[ToolResultBlock(
            tool_use_id="tu-1", content=[{"type": "text", "text": "line one"},
                                         {"type": "text", "text": "line two"}])])
        r, _ = self._runner([[init_msg(), thinking, UserMessage(content="a plain string"),
                              tool_list, assistant(text="ok"), result()]])
        r.start()
        r.enqueue(self._op("x"))
        self.assertTrue(_wait(lambda: _results(r)))
        events = list(r.events())
        self.assertTrue(any(e["kind"] == "thinking" and e["payload"]["length"] == 3
                            for e in events))
        self.assertTrue(any(e["kind"] == "user" and e["payload"]["text"] == "a plain string"
                            for e in events))
        tr = next(e["payload"] for e in events if e["kind"] == "tool_result")
        self.assertEqual(tr["text"], "line one\nline two")

    def test_module_imports_without_the_sdk_installed(self):
        import importlib
        import sys
        saved = {k: v for k, v in sys.modules.items() if k.startswith("claude_agent_sdk")}
        for k in saved:
            sys.modules[k] = None
        try:
            import cousin_lib.runner.sdk as m
            importlib.reload(m)
        finally:
            for k, v in saved.items():
                sys.modules[k] = v
            importlib.reload(m)

    # -- Turn (task 3) -------------------------------------------------------
    def test_the_turn_carries_both_threads_of_a_fold(self):
        r, made = self._runner([[init_msg(), assistant(tool="Bash"), assistant(text="x"), result()]],
                               delay=0.15)
        seen = {}
        orig_close = r._close
        def spy_close(*a, **kw):
            seen["threads"] = r.turn.threads
            return orig_close(*a, **kw)
        r._close = spy_close
        r.start()
        r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r.enqueue(Item("person:sam", "chat", "second", sender="Sam"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events()), timeout=6))
        self.assertEqual(set(seen["threads"]), {"operator:priya", "person:sam"})
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertFalse(r.turn.active)


class TestMirrorError(HermeticCase):
    def test_a_mirror_error_is_an_error_event_and_the_turn_still_lands(self):
        from claude_agent_sdk.types import MirrorErrorMessage
        home = temp_home(self)
        err = MirrorErrorMessage(subtype="mirror_error", data={}, key=None, error="disk full")
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(
            o, [[init_msg(), err, assistant(text="ok"), result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        errors = [e["payload"] for e in r.events() if e["kind"] == "error"]
        self.assertTrue(any(e.get("mirror_error") and "disk full" in e["error"] for e in errors))


if __name__ == "__main__":
    unittest.main()
