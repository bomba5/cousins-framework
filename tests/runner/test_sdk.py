"""SdkRunner against a scripted client: the whole loop, no model."""
import asyncio
import json
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

    # -- commit attribution (tracker #112) --------------------------------
    def test_options_carry_no_settings_by_default(self):
        r, _ = self._runner([])
        self.assertIsNone(r.options().settings)

    def test_options_carry_attribution_settings_when_the_cousin_turns_it_off(self):
        (self.home / "cousin.toml").write_text(
            (self.home / "cousin.toml").read_text() + "commit_attribution = false\n")
        r, _ = self._runner([])
        settings = json.loads(r.options().settings)
        self.assertIs(settings["includeCoAuthoredBy"], False)
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""})

    def test_options_carry_no_settings_when_explicitly_on(self):
        (self.home / "cousin.toml").write_text(
            (self.home / "cousin.toml").read_text() + "commit_attribution = true\n")
        r, _ = self._runner([])
        self.assertIsNone(r.options().settings)

    def test_the_real_transport_puts_it_on_the_cli_argv(self):
        # Not the SDK's own dataclass, the actual argv SubprocessCLITransport
        # would exec: options.settings is a free-form field several SDK
        # layers could still drop before it reaches the CLI (review round 1
        # minor). _cli_path is set by hand so _build_command runs without
        # connect()'s real CLI discovery / subprocess spawn.
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        (self.home / "cousin.toml").write_text(
            (self.home / "cousin.toml").read_text() + "commit_attribution = false\n")
        r, _ = self._runner([])
        transport = SubprocessCLITransport("hi", r.options())
        transport._cli_path = "/bin/true"
        argv = transport._build_command()
        self.assertIn("--settings", argv)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertIs(settings["includeCoAuthoredBy"], False)
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""})

    # -- one turn ----------------------------------------------------------------
    def test_a_turn_records_init_text_tool_and_result_and_closes_the_row(self):
        r, made = self._runner([[init_msg("none"), assistant(tool="Bash"),
                                  assistant(text="done"), result()]])
        r.start()
        receipt = r.enqueue(self._op("hello"))
        # the result is appended, then the row closes (#87): wait for both
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())
                              and r.inbox.get(receipt.inbox_id)["state"] == "done"))
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
        # the result is appended, then the row closes (#87): wait for both
        self.assertTrue(_wait(lambda: len(_results(r)) == 2
                              and r.inbox.get(a.inbox_id)["state"] == "done"))
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
        # the result is appended, then the row closes (#87): wait for both
        self.assertTrue(_wait(lambda: len(_results(r)) == 3
                              and r.inbox.get(c.inbox_id)["state"] == "done"))
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
        with the send hint; unnamed, it is refused (never guessed); named,
        the operator's thread works. send reaches the peer."""
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
        self.assertTrue(err, text)                 # two live threads: never guessed
        self.assertIn("thread=operator:priya", text)
        text, err = tools.call(ctx, "reply", {"text": "on it", "thread": "operator:priya"})
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
        # the result is appended, then the row closes (#87): wait for both
        self.assertTrue(_wait(lambda: _results(r) and r.inbox.get(a.inbox_id)["state"] == "done",
                              timeout=8))
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
        # the result is appended, then the row closes (#87): wait for both
        self.assertTrue(_wait(lambda: len(_results(r)) == 2
                              and r.inbox.get(b.inbox_id)["state"] == "done"))
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
        # the result is appended, then the row closes (#87): wait for both
        self.assertTrue(_wait(lambda: len(_results(r)) == 2
                              and r.inbox.get(b.inbox_id)["state"] == "done"))
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

    # -- a stop claims nothing new (live proofs 09-25, finding 3) ------------
    def test_a_runner_asked_to_stop_claims_no_queued_row(self):
        r, _ = self._runner([])
        r.start()
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        r.begin_stop()                         # the SIGTERM arrived; stop() not yet run
        rec = r.enqueue(self._op("queued at the stop"))
        time.sleep(0.5)
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "queued")
        r.stop(timeout=5)
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "queued")

    def test_a_stopping_runner_folds_nothing_into_its_live_turn(self):
        r, made = self._runner([[init_msg(), "HANG", result()]])
        r.start()
        r.enqueue(self._op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        r.begin_stop()
        rec = r.enqueue(self._op("would fold"))
        time.sleep(0.5)
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "queued")
        self.assertEqual(len(made["client"].queries), 1)
        r.stop(timeout=5)
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "queued")

    def test_a_claim_that_races_the_stop_goes_back(self):
        r, _ = self._runner([])
        rec = r.inbox.put(self._op("raced"))
        real = r.inbox.claim

        def claim(**kw):
            rows = real(**kw)
            r.begin_stop()                     # the stop lands while the claim runs
            return rows
        r.inbox.claim = claim
        self.assertEqual(r._claim(1), [])
        self.assertEqual(r.inbox.get(rec)["state"], "queued")

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


class BackpressureClient(ScriptedClient):
    """The bundled CLI once its stdout is full (#104, measured on SDK
    0.2.159): while more than `cap` messages sit unread (the SDK's
    100-message buffer, then the pipe), the CLI stops reading stdin, so a
    write (a query, or a control request such as interrupt) does not
    return until the reader drains below `cap`. A reader that waits on
    that write never drains it. `give_up_s` bounds the wait so a test of
    the old code fails on its own timeout instead of hanging. `writes`
    is the order writes reached the CLI."""

    def __init__(self, options, scripts, cap=100, give_up_s=15.0, **kw):
        super().__init__(options, scripts, **kw)
        self.cap, self.give_up_s = cap, give_up_s
        self.writes = []
        self.blocked = 0          # writes that had to wait for the reader

    async def _stdin_intake(self):
        deadline = time.monotonic() + self.give_up_s
        if len(self.stream) > self.cap:
            self.blocked += 1
        while len(self.stream) > self.cap and not self.ended and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    async def query(self, prompt, session_id="default"):
        if not isinstance(prompt, str):
            taken = [m async for m in prompt]      # the message leaves the runner now

            async def again():
                for m in taken:
                    yield m
            prompt = again()
        await self._stdin_intake()
        await super().query(prompt, session_id)
        self.writes.append("query")

    async def interrupt(self):
        await self._stdin_intake()
        await super().interrupt()
        self.writes.append("interrupt")


def _long_turn(n=150):
    """A turn that streams `n` messages after a PAUSE: a fold made during
    the PAUSE meets a full stdout."""
    return [init_msg(), assistant(tool="Bash"), "PAUSE"] + \
        [assistant(text="line %d" % i) for i in range(n)] + [result()]


class TestFoldWriteNeverBlocksTheReader(HermeticCase):
    """#118 follow-up to #104: the fold's write must never be awaited on the
    path that reads the CLI's output, or a full stdout deadlocks the turn
    (the 18-39 min stalls)."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def _runner(self, scripts):
        made = {}

        def factory(options):
            made["client"] = BackpressureClient(options, scripts)
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r, made

    def test_a_fold_into_a_full_stdout_keeps_the_reader_reading(self):
        r, made = self._runner([_long_turn()])
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        b = r.enqueue(Item("peer:testa", "chat", "STOP", sender="Testa"))
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "claimed"))
        self.assertTrue(_wait(lambda: made["client"].blocked == 1))
        made["client"].resume()
        # the reader drains the stdout, the write goes through, its echo is read
        self.assertTrue(_wait(lambda: _results(r), timeout=5.0),
                        "the turn stalled: the fold's write blocked the reader")
        res = _results(r)
        self.assertEqual(len(res), 1)
        self.assertEqual(sorted(res[0]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(b.inbox_id)["outcome"], "delivered")
        self.assertEqual(made["client"].writes, ["query", "query"])

    def test_a_fold_write_that_wrote_nothing_fails_the_turn_and_requeues_the_row(self):
        """As before the writer: a fold whose query() raised before a byte
        was written goes back to the queue, and the turn fails."""
        class Refuses(ScriptedClient):
            refused = False

            async def query(self, prompt, session_id="default"):
                if self.queries and not Refuses.refused:     # the fold, once
                    Refuses.refused = True
                    raise CLIConnectionError("ProcessTransport is not ready for writing")
                await super().query(prompt, session_id)
        made = {}

        def factory(options):
            made.setdefault("clients", []).append(Refuses(options, [
                [init_msg(), assistant(tool="Bash"), "PAUSE", assistant(text="x"), result()]]))
            return made["clients"][-1]
        r = SdkRunner(self.home, client_factory=factory, drain_timeout_s=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("clients") and made["clients"][0].paused))
        b = r.enqueue(Item("peer:testa", "chat", "STOP", sender="Testa"))
        # the result is appended, then the rows close (#87): wait for both
        self.assertTrue(_wait(lambda: [x for x in _results(r) if x["is_error"]]
                              and r.inbox.get(a.inbox_id)["state"] == "done"))
        self.assertTrue(any("not ready for writing" in e for e in _errors(r)))
        failed = [x for x in _results(r) if x["is_error"]][0]
        self.assertEqual((failed["inbox_ids"], failed["requeued"]), ([a.inbox_id], [b.inbox_id]))
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "failed")
        made["clients"][0].resume()      # the drain may still be reading it
        # requeued, never failed: a later turn runs it
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "done", timeout=8))

    def test_an_interrupt_row_cut_off_mid_write_by_the_turn_end_is_closed(self):
        """Review of #118: the interrupt's control write is still pending
        when the turn ends (its result arrived meanwhile). The row must not
        stay `claimed`: it is closed delivered, "written as the turn ended"."""
        class SlowInterrupt(ScriptedClient):
            async def interrupt(self):
                await asyncio.Event().wait()     # the control reply never comes
        made = {}

        def factory(options):
            made["client"] = SlowInterrupt(options, [
                [init_msg(), assistant(tool="Bash"), "PAUSE", assistant(text="x"), result()]])
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        stop = r.enqueue(Item("system", "interrupt", "stop", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "claimed"))
        time.sleep(0.3)
        made["client"].resume()
        self.assertTrue(_wait(lambda: _results(r)))
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "done"),
                        "the interrupt row was left claimed")
        row = r.inbox.get(stop.inbox_id)
        self.assertEqual(row["outcome"], "delivered")
        self.assertIn("written as the turn ended", row["detail"])

    def test_a_result_before_a_queued_interrupt_was_sent_is_not_interrupted(self):
        """Review of #118 (minor 3): an interrupt row taken while a fold's
        write is blocked waits behind it; a result that comes first was
        not interrupted, whatever was asked."""
        import threading
        gate = threading.Event()

        class GatedFold(ScriptedClient):
            blocking = False

            async def query(self, prompt, session_id="default"):
                if self.queries and not gate.is_set():
                    self.blocking = True
                    while not gate.is_set():
                        await asyncio.sleep(0.01)
                await super().query(prompt, session_id)
        made = {}

        def factory(options):
            made["client"] = GatedFold(options, [
                [init_msg(), assistant(tool="Bash"), "PAUSE", assistant(text="x"), result()]])
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        self.addCleanup(gate.set)
        r.start()
        r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        b = r.enqueue(Item("peer:testa", "chat", "STOP", sender="Testa"))
        self.assertTrue(_wait(lambda: made["client"].blocking))
        stop = r.enqueue(Item("system", "interrupt", "stop", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "claimed"))
        made["client"].resume()
        self.assertTrue(_wait(lambda: _results(r)))
        self.assertFalse(_results(r)[0]["interrupted"], _results(r)[0])
        self.assertEqual(made["client"].interrupts, 0)
        gate.set()
        self.assertTrue(_wait(lambda: r.inbox.get(b.inbox_id)["state"] == "done", timeout=8))
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "done", timeout=8))

    def test_every_fold_write_that_wrote_nothing_goes_back_to_the_queue(self):
        """Review round 2 of #118: a closed transport refuses every fold of
        one claim without a byte written. Each row goes back exactly once,
        not only the first: a second one (a STOP, say) must not stay
        claimed until the next start."""
        refusals = []

        class Refuses(ScriptedClient):
            async def query(self, prompt, session_id="default"):
                if self.queries and len(refusals) < 2:     # the two folds
                    refusals.append(1)
                    raise CLIConnectionError("ProcessTransport is not ready for writing")
                await super().query(prompt, session_id)
        made = {}

        def factory(options):
            made.setdefault("clients", []).append(Refuses(options, [
                [init_msg(), assistant(tool="Bash"), "PAUSE", assistant(text="x"), result()]]))
            return made["clients"][-1]
        r = SdkRunner(self.home, client_factory=factory, drain_timeout_s=1.0)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("clients") and made["clients"][0].paused))
        # straight into the inbox, no doorbell: both land in one fold's claim
        b_id = r.inbox.put(Item("peer:testa", "chat", "one", sender="Testa"))
        c_id = r.inbox.put(Item("peer:testa", "chat", "STOP", sender="Testa"))
        self.assertTrue(_wait(lambda: len(refusals) == 2))
        made["clients"][0].resume()
        self.assertTrue(_wait(lambda: [x for x in _results(r) if x["is_error"]]))
        failed = [x for x in _results(r) if x["is_error"]][0]
        self.assertEqual(sorted(failed["requeued"]), sorted([b_id, c_id]))
        for i in (b_id, c_id):          # requeued, then run by a later turn
            self.assertTrue(_wait(lambda: r.inbox.get(i)["state"] == "done", timeout=10),
                            "fold %d was left claimed" % i)
            self.assertEqual(r.inbox.get(i)["outcome"], "delivered")

    def test_an_interrupt_row_behind_a_blocked_fold_is_written_after_it(self):
        """One writer, in order: the fold that was taken first reaches the
        CLI first, then the interrupt; the reader never waits on either."""
        r, made = self._runner([_long_turn()])
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        b = r.enqueue(Item("operator:priya", "chat", "second", sender="Priya"))
        self.assertTrue(_wait(lambda: made["client"].blocked == 1))
        stop = r.enqueue(Item("system", "interrupt", "stop", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "claimed"))
        made["client"].resume()
        self.assertTrue(_wait(lambda: _results(r), timeout=5.0),
                        "the turn stalled: a write blocked the reader")
        self.assertTrue(_wait(lambda: r.inbox.get(stop.inbox_id)["state"] == "done"))
        self.assertEqual(r.inbox.get(stop.inbox_id)["outcome"], "delivered")
        self.assertEqual(made["client"].writes, ["query", "query", "interrupt"])
        res = _results(r)
        self.assertEqual(sorted(res[0]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))
        self.assertTrue(res[0]["interrupted"])


class TestTurnWriter(HermeticCase):
    """The writer itself: FIFO, one task, a clean close."""

    def test_writes_run_in_order_and_close_drops_the_rest_with_no_task_left(self):
        from cousin_lib.runner.base import RunnerError
        from cousin_lib.runner.sdk import _Job, _Writer
        order, dropped, reports = [], [], []

        async def go():
            gate = asyncio.Event()
            w = _Writer(reports.append)

            def job(name, wait=False):
                async def fn():
                    order.append(name)
                    if wait:
                        await gate.wait()
                    return name
                return _Job(fn, on_dropped=lambda started: dropped.append((name, started)))
            first = w.submit(job("a"))
            w.submit(job("b", wait=True))       # blocks: c and d wait behind it
            w.submit(job("c")); w.submit(job("d"))
            self.assertEqual(await first.future, "a")
            await asyncio.sleep(0.05)
            self.assertEqual(order, ["a", "b"])
            await w.close()
            self.assertTrue(w._task.done())
            # b was cut off mid-write: said as started, then c and d never begun
            self.assertEqual(dropped, [("b", True), ("c", False), ("d", False)])
            with self.assertRaises(RunnerError):
                w.submit(job("e"))
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            self.assertEqual(others, [])
        asyncio.run(go())
        self.assertEqual(reports, [])

    def test_a_raising_handler_is_reported_and_the_writer_goes_on(self):
        from cousin_lib.runner.sdk import _Job, _Writer
        reports, done = [], []

        async def go():
            w = _Writer(reports.append)

            async def one():
                return 1

            def bad(_value):
                raise ValueError("inbox gone")
            w.submit(_Job(one, on_ok=bad))
            last = w.submit(_Job(one, on_ok=done.append))
            await last.future
            await asyncio.sleep(0)
            await w.close()
        asyncio.run(go())
        self.assertEqual(done, [1])
        self.assertEqual(reports, ["writer: ValueError: inbox gone"])


class TestTurnWriterEdges(HermeticCase):
    """Review of #118: the writer's close and its failures."""

    def test_a_raising_on_dropped_does_not_orphan_the_jobs_behind_it(self):
        from cousin_lib.runner.sdk import _Job, _Writer
        dropped, reports = [], []

        async def go():
            w = _Writer(reports.append)
            never = asyncio.Event()

            async def blocks():
                await never.wait()

            def bad(_started):
                raise ValueError("inbox gone")
            w.submit(_Job(blocks, on_dropped=bad))
            w.submit(_Job(blocks, on_dropped=lambda st: dropped.append(("b", st))))
            w.submit(_Job(blocks, on_dropped=lambda st: dropped.append(("c", st))))
            await asyncio.sleep(0.02)
            await w.close()
        asyncio.run(go())
        self.assertEqual(dropped, [("b", False), ("c", False)])
        self.assertEqual(reports, ["writer: ValueError: inbox gone"])

    def test_close_reraises_a_cancellation_of_the_turn_after_dropping_the_jobs(self):
        from cousin_lib.runner.sdk import _Job, _Writer
        dropped = []

        async def go():
            w = _Writer(lambda text: None)
            never = asyncio.Event()

            async def blocks():
                await never.wait()
            w.submit(_Job(blocks, on_dropped=lambda st: dropped.append(st)))
            await asyncio.sleep(0.02)

            async def turn():
                await w.close()
                return "not cancelled"
            t = asyncio.ensure_future(turn())
            await asyncio.sleep(0)       # the turn is inside close()
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        asyncio.run(go())
        self.assertEqual(dropped, [True])

    def test_a_job_whose_awaiter_cancelled_its_future_does_not_kill_the_writer(self):
        from cousin_lib.runner.sdk import _Job, _Writer

        async def go():
            w = _Writer(lambda text: None)

            async def slow():
                await asyncio.sleep(0.05)
                return "late"

            async def fine():
                return "ok"
            first = w.submit(_Job(slow))
            first.future.cancel()                # its awaiter gave up
            last = w.submit(_Job(fine))
            self.assertEqual(await asyncio.wait_for(last.future, 2.0), "ok")
            await w.close()
        asyncio.run(go())

    def test_submit_to_a_writer_whose_task_died_is_refused(self):
        from cousin_lib.runner.base import RunnerError
        from cousin_lib.runner.sdk import _Job, _Writer

        async def go():
            w = _Writer(lambda text: None)
            w._task.cancel()                     # died, and nobody closed it
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            async def fine():
                return "ok"
            with self.assertRaises(RunnerError):
                w.submit(_Job(fine))
            await w.close()
        asyncio.run(go())

    def test_a_cancelled_error_from_inside_the_client_fails_the_job_and_the_writer_goes_on(self):
        from cousin_lib.runner.sdk import _Job, _Writer
        errors, done, reports = [], [], []

        async def go():
            w = _Writer(reports.append)

            async def sdk_cancels():
                raise asyncio.CancelledError()     # an anyio scope inside the SDK, say

            async def fine():
                return "ok"
            w.submit(_Job(sdk_cancels, on_error=errors.append))
            last = w.submit(_Job(fine, on_ok=done.append))
            self.assertEqual(await last.future, "ok")
            await w.close()
        asyncio.run(go())
        self.assertEqual([type(e).__name__ for e in errors], ["RunnerError"])
        self.assertEqual(done, ["ok"])
        self.assertTrue(any("cancelled inside the client" in r for r in reports), reports)


def _broken_render(real):
    """envelope.render_message that raises for a body "broken", as a broken
    attachment does."""
    def render(item, **kw):
        if item.body == "broken":
            raise ValueError("attachment unreadable")
        return real(item, **kw)
    return render


class TestUnrenderableRow(HermeticCase):
    """#118 review: a row that cannot be rendered is not transient. It is
    closed FAILED with the reason, never requeued, never left claimed."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        from unittest import mock
        from cousin_lib.runner import envelope
        patch = mock.patch.object(envelope, "render_message",
                                  _broken_render(envelope.render_message))
        patch.start(); self.addCleanup(patch.stop)

    def test_a_first_row_that_cannot_be_rendered_is_failed_and_the_runner_goes_on(self):
        made = {}

        def factory(options):
            made["client"] = ScriptedClient(options, [])
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        bad = r.enqueue(Item("operator:priya", "chat", "broken", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(bad.inbox_id)["state"] == "done"),
                        "the unrenderable first row was left claimed")
        row = r.inbox.get(bad.inbox_id)
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["detail"], "could not be rendered: ValueError: attachment unreadable")
        self.assertEqual(made["client"].queries, [])       # nothing reached the CLI
        good = r.enqueue(Item("operator:priya", "chat", "fine", sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(good.inbox_id)["state"] == "done", timeout=8))
        self.assertEqual(r.inbox.get(good.inbox_id)["outcome"], "delivered")

    def test_a_fold_that_cannot_be_rendered_is_failed_and_the_rest_fold_at_once(self):
        made = {}

        def factory(options):
            made["client"] = ScriptedClient(options, [
                [init_msg(), assistant(tool="Bash"), "PAUSE", assistant(text="x"), result()]])
            return made["client"]
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        requeued = []
        real_requeue = r.inbox.requeue

        def spy(inbox_id):
            requeued.append(inbox_id)
            return real_requeue(inbox_id)
        r.inbox.requeue = spy
        r.start()
        a = r.enqueue(Item("operator:priya", "chat", "first", sender="Priya"))
        self.assertTrue(_wait(lambda: made.get("client") and made["client"].paused))
        # one claim: the operator row ranks first, the two peers after it
        bad = r.inbox.put(Item("operator:priya", "chat", "broken", sender="Priya"))
        b = r.inbox.put(Item("peer:testa", "chat", "one", sender="Testa"))
        c = r.inbox.put(Item("peer:testa", "chat", "two", sender="Testa"))
        self.assertTrue(_wait(lambda: r.inbox.get(bad)["state"] == "done"))
        row = r.inbox.get(bad)
        self.assertEqual((row["outcome"], row["detail"]),
                         ("failed", "could not be rendered: ValueError: attachment unreadable"))
        self.assertNotIn(bad, requeued)
        # the rest of the claim folds at once: never requeued
        self.assertNotIn(b, requeued); self.assertNotIn(c, requeued)
        self.assertTrue(_wait(lambda: len(made["client"].queries) == 3))
        made["client"].resume()
        self.assertTrue(_wait(lambda: all(r.inbox.get(i)["state"] == "done"
                                          for i in (a.inbox_id, b, c)), timeout=8))
        self.assertEqual([r.inbox.get(i)["outcome"] for i in (a.inbox_id, b, c)],
                         ["delivered"] * 3)
        res = [x for x in _results(r) if not x["is_error"]]
        self.assertEqual(sorted(res[0]["inbox_ids"]), sorted([a.inbox_id, b, c]))


class TestRefusedSubmit(HermeticCase):
    def test_a_fold_the_writer_refuses_is_in_no_list_and_goes_back(self):
        """#118 review item 4: a submit refused (the writer ended) leaves
        the row out of open_rows and requeues it and the rest."""
        from cousin_lib.runner.base import RunnerError
        from cousin_lib.runner.sdk import _sdk, _Writer
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, []))
        ids = [r.inbox.put(Item("peer:testa", "chat", body, sender="Testa"))
               for body in ("one", "two")]
        open_rows = []

        async def go():
            r._writer = _Writer(lambda text: None)
            await r._writer.close()
            r._writer._closed = False          # as a writer whose task ended unclosed
            with self.assertRaises(RunnerError):
                await r._fold(_sdk(), open_rows)
        asyncio.run(go())
        self.assertEqual(open_rows, [])
        self.assertEqual([r.inbox.get(i)["state"] for i in ids], ["queued", "queued"])


class TestDeadWriter(HermeticCase):
    """#118 review round 4: a writer whose task ended on its own."""

    def _runner(self):
        r = SdkRunner(temp_home(self), client_factory=lambda o: ScriptedClient(o, []))
        return r

    def test_an_interrupt_row_a_dead_writer_refuses_goes_back_and_the_flag_clears(self):
        from cousin_lib.runner.base import RunnerError
        from cousin_lib.runner.sdk import _Writer
        r = self._runner()
        stop = r.inbox.put(Item("system", "interrupt", "stop", sender="Priya"))

        async def go():
            r.machine.to("running", "turn")
            r._live = True
            r._writer = _Writer(lambda t: None)
            r._writer._task.cancel()
            await asyncio.sleep(0); await asyncio.sleep(0)
            with self.assertRaises(RunnerError):      # the turn fails, as before
                await r._take_interrupts()
            await r._writer.close()
        asyncio.run(go())
        self.assertEqual(r.inbox.get(stop)["state"], "queued")
        self.assertFalse(r._interrupt_requested)

    def test_the_reader_fails_the_turn_as_soon_as_the_writer_has_ended(self):
        from cousin_lib.runner.base import RunnerError
        from cousin_lib.runner.sdk import _Writer
        r = self._runner()

        async def go():
            r._writer = _Writer(lambda t: None)
            r._raise_write_error()                    # alive: nothing to raise
            r._writer._task.cancel()
            await asyncio.sleep(0); await asyncio.sleep(0)
            with self.assertRaisesRegex(RunnerError, "writer ended"):
                r._raise_write_error()
            await r._writer.close()
            r._raise_write_error()                    # closed on purpose: nothing
        asyncio.run(go())


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


class TestTaskEvents(HermeticCase):
    """#129: a background task's lifecycle reaches the stream with the
    fields the pane's task list shows, and nothing else of the raw payload
    (no prompt, no output file, no output)."""

    def _messages(self):
        from claude_agent_sdk._internal.message_parser import parse_message
        base = {"type": "system", "uuid": "u", "session_id": "s-1"}
        raw = [
            dict(base, subtype="task_started", task_id=" t1 ", tool_use_id="tu-9",
                 description="  Audit the docs  ", task_type="local_agent",
                 prompt="SECRET PROMPT"),
            dict(base, subtype="task_progress", task_id="t1", description="Audit the docs",
                 last_tool_name="Grep", tool_use_id="tu-9",
                 usage={"total_tokens": 1200, "tool_uses": 3, "duration_ms": 4000,
                        "extra": "x"}),
            dict(base, subtype="task_updated", task_id="t1", patch={"end_time": 5}),
            dict(base, subtype="task_updated", task_id="t1",
                 patch={"status": "killed", "result": "SECRET OUTPUT"}),
            dict(base, subtype="task_notification", task_id="t1", status="stopped",
                 output_file="/tmp/SECRET", summary="  " + "s" * 400 + "  ",
                 usage={"total_tokens": 1}),
        ]
        return [parse_message(m) for m in raw]

    def test_each_task_message_is_one_bounded_system_event(self):
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(
            o, [[init_msg()] + self._messages() + [assistant(text="ok"), result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: _results(r)))
        got = [e["payload"] for e in r.events() if e["kind"] == "system"
               and str(e["payload"].get("subtype", "")).startswith("task_")]
        self.assertEqual(got, [
            {"subtype": "task_started", "task_id": "t1", "description": "Audit the docs",
             "task_type": "local_agent", "tool_use_id": "tu-9"},
            {"subtype": "task_progress", "task_id": "t1", "last_tool_name": "Grep",
             "usage": {"total_tokens": 1200, "tool_uses": 3, "duration_ms": 4000}},
            {"subtype": "task_updated", "task_id": "t1"},
            {"subtype": "task_updated", "task_id": "t1", "status": "killed"},
            {"subtype": "task_notification", "task_id": "t1", "status": "stopped",
             "summary": "s" * 300},
        ])
        self.assertNotIn("SECRET", json.dumps(got))

    def test_a_malformed_task_message_still_leaves_an_event(self):
        from cousin_lib.runner.sdk import _task_payload
        self.assertEqual(_task_payload("task_started", None),
                         {"subtype": "task_started", "task_id": None, "description": None,
                          "task_type": None, "tool_use_id": None})
        self.assertEqual(_task_payload("task_progress", {"task_id": "t", "usage": "x"}),
                         {"subtype": "task_progress", "task_id": "t", "last_tool_name": None,
                          "usage": {}})
        self.assertEqual(_task_payload("task_updated", {"task_id": "t", "patch": None}),
                         {"subtype": "task_updated", "task_id": "t"})


if __name__ == "__main__":
    unittest.main()
