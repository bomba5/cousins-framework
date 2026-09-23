"""SdkRunner: a cousin on the Claude Agent SDK, no terminal.

The only module in the framework that imports claude_agent_sdk, and it
does so lazily so the core stays importable without the extra. The
client lives for the runner's life (phase 0 finding 4: it survives a
ten-minute idle on both auth lanes). The auth lane is the presence of
ANTHROPIC_API_KEY in `options.env` and nothing else; `apiKeySource`
from every init message goes to the event stream so a cousin on the
wrong lane is visible.

One thread owns one asyncio loop, and that loop owns the client. The
loop's shape is FakeRunner's (the reference runner): claim one row,
run one turn, fold operator/person chat that lands mid-turn into it,
close every consumed row with the turn's one result, and route every
failure through `_fail_turn` so nothing dies silently.
"""
import asyncio
import threading
import time
import uuid
from pathlib import Path

from cousin_lib.delivery import DELIVERED, FAILED, QUEUED, Item
from cousin_lib.runner import envelope, wake
from cousin_lib.runner.base import Receipt, RunnerError
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream

FOLDED_KINDS = ("operator", "person")


def _sdk():
    try:
        import claude_agent_sdk
    except ImportError as err:  # pragma: no cover - exercised by the import test
        raise RunnerError("claude-agent-sdk is not installed; pip install"
                          " -e '.[sdk]'") from err
    return claude_agent_sdk


async def _one(message):
    """One envelope as the one-item async iterable `ClaudeSDKClient.query`
    takes: its prompt is `str | AsyncIterable[dict]`, and a bare dict would
    reach its `async for` and raise TypeError."""
    yield message


def _default_factory(options):
    return _sdk().ClaudeSDKClient(options=options)


class SdkRunner:
    def __init__(self, home, *, client_factory=None, api_key=None, model=None,
                 cwd=None, turn_timeout_s=600.0):
        self.home = Path(home)
        self.api_key = api_key
        self.model = model
        self.cwd = Path(cwd) if cwd else self.home
        self.turn_timeout_s = float(turn_timeout_s)
        self.client_factory = client_factory or _default_factory
        self.session_id = "sdk-" + uuid.uuid4().hex[:8]
        self.inbox = Inbox(self.home)
        self.stream = EventStream(self.home, self.session_id)
        self.machine = StateMachine(on_change=self._on_state)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._client = None
        self._interrupt_requested = False

    # -- options -----------------------------------------------------------
    def options(self):
        sdk = _sdk()
        env = {"ANTHROPIC_API_KEY": self.api_key} if self.api_key else {}
        return sdk.ClaudeAgentOptions(cwd=str(self.cwd), model=self.model, env=env,
                                      permission_mode="bypassPermissions",
                                      setting_sources=[])

    def _on_state(self, old, new, detail):
        self.stream.append("state", {"from": old, "to": new, "detail": detail})

    # -- Runner protocol ---------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self, *, timeout=30.0):
        if self.machine.state == "stopped":
            return
        # A running turn is interrupted first, so the join below does not
        # wait on a turn nobody will end (FakeRunner does the same).
        self.interrupt()
        self._stop.set()
        wake.poke(self.home)
        if self._thread is not None:
            self._thread.join(timeout)
        with self._lock:
            if self.machine.state != "stopped":
                self.machine.to("stopped")

    def state(self):
        return self.machine.state

    def enqueue(self, item):
        if not isinstance(item, Item):
            raise TypeError("enqueue() takes a delivery.Item")
        inbox_id = self.inbox.put(item)
        wake.poke(self.home)
        return Receipt(inbox_id=inbox_id, outcome=QUEUED)

    def interrupt(self):
        """Schedule `client.interrupt()` on the runner's loop. The check
        and the schedule happen under the lock that guards `running ->
        idle`, so the loop is still running the turn when the callback is
        queued on it; a loop that closed anyway gets the coroutine closed
        here, never left unawaited."""
        with self._lock:
            if self.machine.state != "running" or self._loop is None \
                    or self._client is None:
                return False
            self._interrupt_requested = True
            coro = self._client.interrupt()
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:  # the loop closed under us
                coro.close()
                return False
        fut.add_done_callback(self._interrupt_done)
        return True

    def _interrupt_done(self, fut):
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            self.stream.append("error", {"error": "interrupt: %s: %s"
                                         % (type(exc).__name__, exc)})

    def rollover(self, reason):
        return {"ok": False, "reason": "rollover arrives in phase 4"}

    def events(self, after=None):
        return self.stream.tail(after=after)

    def unsupported(self):
        return []

    # -- the loop ------------------------------------------------------------
    def _run_loop(self):
        # asyncio.Runner's close cancels leftover tasks, finalizes async
        # generators and joins the default executor before closing the loop.
        with asyncio.Runner() as runner:
            self._loop = runner.get_loop()
            runner.run(self._main())

    async def _main(self):
        try:
            self._client = self.client_factory(self.options())
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 - a runner that cannot connect says so
            self._fail_connect(exc)
            await self._disconnect()
            return
        try:
            with wake.Listener(self.home) as listener:
                while not self._stop.is_set():
                    try:
                        rows = self.inbox.claim(limit=1, claimant=self.session_id)
                    except Exception as exc:  # noqa: BLE001 - a store failure is never silence
                        self._fail_turn([], exc)
                        await asyncio.sleep(0.2)  # a wedged store must not spin the loop
                        continue
                    if not rows:
                        await asyncio.get_running_loop().run_in_executor(
                            None, listener.wait, 0.2)
                        continue
                    try:
                        await self._turn(rows[0])
                    except Exception as exc:  # noqa: BLE001 - the success tail can still raise
                        # `[]`: its rows may already be closed (FakeRunner._fail_turn)
                        self._fail_turn([], exc)
        finally:
            await self._disconnect()

    async def _disconnect(self):
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001 - a dying client must not mask the stop
            pass

    def _fail_connect(self, exc):
        """The client never came up: the error goes to the stream and the
        machine stays `errored` (nothing will run a turn; `stop()` moves
        it on)."""
        message = "%s: %s" % (type(exc).__name__, exc)
        self.stream.append("error", {"error": message})
        with self._lock:
            if self.machine.state in ("idle", "running"):
                self.machine.to("errored", message)

    def _row_item(self, row):
        return Item(thread_id=row["thread_id"], source=row["source"], body=row["body"],
                    sender=row["sender"], attachments=tuple(row["attachments"]),
                    context=row["context"], message_id=row["message_id"])

    async def _query(self, row):
        message = envelope.render_message(self._row_item(row))
        # The SDK's str path sets this key and its iterable path does not.
        message.setdefault("parent_tool_use_id", None)
        await self._client.query(_one(message))

    def _fold_midturn(self, consumed):
        """Operator/person chat that arrived during the turn is query()'d
        into it (finding 1); anything else goes back to the queue."""
        folded = []
        for row in self.inbox.claim(limit=10, claimant=self.session_id):
            kind = row["thread_id"].partition(":")[0]
            if row["source"] == "chat" and kind in FOLDED_KINDS:
                consumed.append(row)
                folded.append(row)
            else:
                self.inbox.requeue(row["id"])
        return folded

    def _fail_turn(self, consumed, exc):
        """FakeRunner._fail_turn's rules: `consumed` is only rows this call
        may safely close; `errored` only from idle/running and `errored ->
        idle` only from errored, so a machine `stop()` forced to `stopped`
        is never touched."""
        message = "%s: %s" % (type(exc).__name__, exc)
        self.stream.append("error", {"error": message})
        with self._lock:
            if self.machine.state in ("idle", "running"):
                self.machine.to("errored", message)
        for row in consumed:
            self.inbox.done(row["id"], FAILED, message)
        self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                      "interrupted": self._interrupt_requested,
                                      "is_error": True, "num_turns": 0,
                                      "total_cost_usd": None, "session_id": None})
        with self._lock:
            if self.machine.state == "errored":
                self.machine.to("idle", "recovered")

    async def _turn(self, first):
        consumed = [first]
        self._interrupt_requested = False
        is_error, num_turns, cost, result_session = False, 0, None, None
        try:
            sdk = _sdk()
            with self._lock:
                self.machine.to("running", "turn")
            self.stream.append("turn_start", {"inbox_ids": [first["id"]],
                                              "bodies": [first["body"]],
                                              "thread_id": first["thread_id"]})
            await self._query(first)
            deadline = time.monotonic() + self.turn_timeout_s
            responses = self._client.receive_response()
            it = responses.__aiter__()
            try:
                while True:
                    # The deadline bounds every wait for the next message,
                    # so a stream that goes silent cannot hang the turn.
                    remaining = deadline - time.monotonic()
                    try:
                        msg = await asyncio.wait_for(it.__anext__(), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise RunnerError("turn exceeded %.0fs"
                                          % self.turn_timeout_s) from None
                    self._record(sdk, msg)
                    if isinstance(msg, sdk.ResultMessage):
                        is_error = bool(msg.is_error)
                        num_turns = msg.num_turns
                        cost = msg.total_cost_usd
                        result_session = msg.session_id
                        break
                    for row in self._fold_midturn(consumed):
                        await self._query(row)
            finally:
                # `break` leaves the generator suspended; close it here, on
                # this loop, rather than leave it to the garbage collector.
                aclose = getattr(responses, "aclose", None)
                if aclose is not None:
                    await aclose()
            # No fold after the result: a row that landed after it was
            # never query()'d, so it stays queued and starts the next turn.
        except Exception as exc:  # noqa: BLE001 - a raising turn body is recorded, not lost
            self._fail_turn(consumed, exc)
            return

        # Success tail: a failure here propagates to `_main`'s outer guard,
        # which calls `_fail_turn([], exc)` (these rows may already be closed).
        interrupted = self._interrupt_requested
        outcome = FAILED if (is_error and not interrupted) else DELIVERED
        for row in consumed:
            self.inbox.done(row["id"], outcome, "turn %s" % (result_session or self.session_id))
        self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                      "interrupted": interrupted,
                                      "is_error": is_error, "num_turns": num_turns,
                                      "total_cost_usd": cost, "session_id": result_session})
        with self._lock:
            if self.machine.state == "running":
                self.machine.to("idle", "turn done")

    def _record(self, sdk, msg):
        if isinstance(msg, sdk.SystemMessage):
            if msg.subtype == "init":
                d = msg.data or {}
                self.stream.append("session_init", {"apiKeySource": d.get("apiKeySource"),
                                                    "model": d.get("model"),
                                                    "session_id": d.get("session_id")})
            else:
                self.stream.append("system", {"subtype": msg.subtype})
        elif isinstance(msg, sdk.AssistantMessage):
            for block in msg.content:
                if isinstance(block, sdk.TextBlock):
                    self.stream.append("text", {"text": block.text})
                elif isinstance(block, sdk.ToolUseBlock):
                    self.stream.append("tool", {"id": block.id, "name": block.name,
                                                "input": block.input})
        elif isinstance(msg, sdk.UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, sdk.ToolResultBlock):
                    text = block.content if isinstance(block.content, str) else ""
                    self.stream.append("tool_result", {"tool_use_id": block.tool_use_id,
                                                       "is_error": bool(block.is_error),
                                                       "text": text[:2000]})
        elif isinstance(msg, sdk.ResultMessage):
            pass   # the caller closes the turn from it
        else:
            self.stream.append("other", {"type": type(msg).__name__})
