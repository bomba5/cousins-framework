"""SdkRunner: a cousin on the Claude Agent SDK, no terminal.

The only module in the framework that imports claude_agent_sdk, and it
does so lazily so the core stays importable without the extra. The
client lives for the runner's life (phase 0 finding 4: it survives a
ten-minute idle on both auth lanes). The auth lane is the presence of
ANTHROPIC_API_KEY in `options.env` and nothing else; `apiKeySource`
from every init message goes to the event stream so a cousin on the
wrong lane is visible.

One thread owns one asyncio loop, and that loop owns the client. The
loop's shape is FakeRunner's (the reference runner): claim one row, run
one turn, fold operator/person chat that lands mid-turn into it, close
every consumed row with a result, and route every failure through
`_fail_turn` so nothing dies silently.

A turn is not 1:1 with a CLI turn (phase 0 finding 1). The CLI is
started with `--replay-user-messages`, so it echoes every user message
it CONSUMES as a UserMessage in the stream, and the echo is the only
proof a row reached the model: a row is closed by the first
ResultMessage after its echo, never by one before it. A row folded in
while the model was finishing its answer is not echoed before that
answer's result; the CLI takes it up as a turn of its own, and the
runner reads on to that turn's result and closes the row with it. So a
runner turn can emit more than one `result` event, and none is left in
the stream for the next turn to misread.
"""
import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from cousin_lib import boot, session, usage
from cousin_lib.delivery import DELIVERED, FAILED, QUEUED, DeliveryError, Item, parse_thread
from cousin_lib.runner import envelope, extract, hooks, rollover, tools, wake
from cousin_lib.runner.base import FOLDED_KINDS, Receipt, RunnerError, folds_into_turn
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from cousin_lib.runner.turn import Turn


def _sdk():
    try:
        import claude_agent_sdk
    except ImportError as err:  # pragma: no cover - exercised by the import test
        raise RunnerError("claude-agent-sdk is not installed; pip install"
                          " -e '.[sdk]'") from err
    return claude_agent_sdk


_END = object()
# A turn is live while the model runs it, including while it waits on a
# permission: every exit from either state (errored, idle, stopped) is legal.
LIVE_STATES = ("running", "waiting_permission")


class _NotWritten(Exception):
    """query() raised before the row reached the transport: the row is
    requeued, never failed, because the model never saw it."""

    def __init__(self, row, cause):
        super().__init__(str(cause))
        self.row, self.cause = row, cause


def _nothing_written(sdk, exc):
    """True when the SDK raised before its transport wrote a byte: every
    check in `SubprocessCLITransport.write` (not ready, process ended,
    earlier exit error) raises CLIConnectionError with no cause, or
    caused by another CLIConnectionError; only a failed send carries the
    OS error that interrupted it, and that one may have written part."""
    if not isinstance(exc, sdk.CLIConnectionError):
        return False
    return exc.__cause__ is None or isinstance(exc.__cause__, sdk.CLIConnectionError)


async def _next_by(it, deadline, overrun):
    """The next message of a response, or `_END`, bounded by one deadline
    (the drain's)."""
    try:
        return await asyncio.wait_for(it.__anext__(), timeout=deadline - time.monotonic())
    except StopAsyncIteration:
        return _END
    except asyncio.TimeoutError:
        raise RunnerError(overrun) from None


async def _aclose(responses):
    """Close a response generator on this loop (a `break` leaves it
    suspended); an AsyncIterator without `aclose` is left alone."""
    aclose = getattr(responses, "aclose", None)
    if aclose is not None:
        await aclose()


def _pressure_reason(context_usage):
    """The rollover reason for a pressure reading: the percentage when the
    reading has one, else the token count that pulled the trigger."""
    try:
        return "context pressure %d%%" % int(context_usage["percentage"])
    except (KeyError, TypeError, ValueError):
        return "context pressure %s tokens" % (context_usage or {}).get("totalTokens")


def _default_factory(options):
    return _sdk().ClaudeSDKClient(options=options)


def _tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


class SdkRunner:
    # After this many consecutive failed turns the loop waits before its
    # next claim: backoff_base_s, doubling, capped; a good turn resets it.
    backoff_after = 3
    backoff_base_s = 1.0
    backoff_cap_s = 30.0
    # How often a live turn looks for operator/person rows to fold in, and
    # how long an idle loop sleeps when the doorbell is a Poller.
    poll_s = 0.2

    def __init__(self, home, *, client_factory=None, api_key=None, model=None,
                 cwd=None, idle_timeout_s=600.0, turn_timeout_s=None,
                 drain_timeout_s=30.0, policy=None, registry=None, handoff_deadline_s=None):
        self.home = Path(home)
        # the tools and the model's own commands find the cousin and the
        # install through these; cousin-runner exports them, and a runner
        # built directly sets whichever is unset
        from cousin_lib.runner.main import export_environment
        export_environment(self.home, overwrite=False)
        self.api_key = api_key
        self.model = model
        self.cwd = Path(cwd) if cwd else self.home
        self.idle_timeout_s = float(idle_timeout_s)
        self.turn_timeout_s = None if turn_timeout_s is None else float(turn_timeout_s)
        self.drain_timeout_s = float(drain_timeout_s)
        self.client_factory = client_factory or _default_factory
        self.session_id = "sdk-" + uuid.uuid4().hex[:8]
        self.inbox = Inbox(self.home)
        self.stream = EventStream(self.home, self.session_id)
        self.machine = StateMachine(on_change=self._on_state)
        self.turn = Turn()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._client = None
        self._interrupt_requested = False
        self._live = False       # the CLI is generating for this turn (see _interrupt_turn)
        self._turn_seq = 0
        self._resume_id = None   # the last session_id an init or result named
        self._client_id = None   # a fresh uuid per connect (usage.record's client key)
        self._lane = "unknown"   # from the last session_init's apiKeySource
        self._failures = 0       # consecutive failed turns
        self._backed_off = 0     # the failure count the last backoff was for
        self._last_fold = 0.0    # when the live turn last looked for rows to fold
        self.fatal = None        # why the worker gave up (a connect failure), else None
        self._fallback_said = False   # the registry-fallback notice, once per runner
        # (row, envelope text) for every row written this runner turn, added
        # BEFORE its query: the prompt hook can fire before the echo.
        self._sent = []
        self.policy = policy if policy is not None else Policy.load(self.home)
        self.registry = registry
        slug, name = self._identity()
        from cousin_lib.runner.main import root_for
        self.root = root_for(self.home)
        self.tool_context = tools.ToolContext(home=self.home, slug=slug, name=name,
                                              root=self.root, turn=self.turn,
                                              policy=self.policy, stream=self.stream,
                                              registry=registry)
        from cousin_lib.runner.session_store import SqliteSessionStore
        self.session_store = SqliteSessionStore(self.home)
        # The rollover (rollover.py): the handoff tool hands its summary to
        # the box, which the rollover awaits; pressure is read after every
        # result, held back by the hysteresis after a rollover.
        self.handoff_deadline_s = float(handoff_deadline_s or rollover.HANDOFF_DEADLINE_S)
        self.rollover_at_percent = float(self._agent_value("rollover_at_percent",
                                                           rollover.ROLLOVER_AT_PERCENT))
        self.handoff_box = rollover.HandoffBox()
        self.hysteresis = rollover.Hysteresis()
        self.tool_context.on_handoff = self.handoff_box.set

    def _identity(self):
        """(slug, name) from cousin.toml; the directory name when it lacks them."""
        from cousin_lib.config import CousinConfig
        try:
            cfg = CousinConfig.load(self.home)
            return cfg.slug, cfg.name
        except Exception:  # noqa: BLE001 - a thin toml still runs; the dir names it
            return self.home.name, self.home.name.capitalize()

    def _agent_value(self, key, default):
        """A value from cousin.toml [agent], else default."""
        import tomllib
        try:
            agent = tomllib.loads((self.home / "cousin.toml").read_text()).get("agent") or {}
        except (OSError, tomllib.TOMLDecodeError):
            return default
        return agent.get(key, default)

    # -- options -----------------------------------------------------------
    def options(self, *, resume=None):
        sdk = _sdk()
        env = {"ANTHROPIC_API_KEY": self.api_key} if self.api_key else {}
        # The tools and hooks are in-process: no settings file is read
        # (setting_sources=[]) and none is written; the policy is a
        # PreToolUse hook, since bypassPermissions skips can_use_tool.
        server = tools.build_tool_server(self.tool_context, self.registry,
                                         on_fallback=self._registry_fallback)
        hook_table = hooks.build_hooks(self.home, slug=self.tool_context.slug, root=self.root,
                                       machine=self.machine, stream=self.stream,
                                       policy=self.policy, lock=self._lock,
                                       body_for_prompt=self._body_for_prompt,
                                       request_rollover=self._request_rollover)
        # The composed prompt (prompt.py): byte-stable across generations,
        # so a rollover and a restart keep the cache (phase 0 finding 3).
        # With snapshot=True a resumed session keeps the prompt it first
        # recorded, so an edit to identity files lands at the next rollover.
        from cousin_lib.runner import prompt
        system_prompt = prompt.system_prompt_option(self.home, root=self.root,
                                                    registry=self.tool_context.registry)
        # replay-user-messages: the echo is how a turn knows which rows the
        # model actually took in (see the module docstring).
        return sdk.ClaudeAgentOptions(cwd=str(self.cwd), model=self.model, env=env,
                                      permission_mode="bypassPermissions",
                                      setting_sources=[], resume=resume,
                                      system_prompt=system_prompt, session_store=self.session_store,
                                      mcp_servers={"cousin": server}, hooks=hook_table,
                                      extra_args={"replay-user-messages": None})

    def _registry_fallback(self, payload):
        """build_tool_server found no registry and used the shipped
        default: say so once per runner, not on every (re)connect."""
        if not self._fallback_said:
            self._fallback_said = True
            self.stream.append("policy", payload)

    def _body_for_prompt(self, prompt):
        """The body the prompt hook searches: that of the row whose
        envelope text is the prompt, among the rows written this runner
        turn, echoed or not; the newest row is not it when the hook
        fires before an echo. The CLI builds the prompt from the text
        blocks joined with "\n" and trimmed (image blocks dropped), so
        both sides are compared stripped: first equal, else the longest
        envelope the prompt starts with (an attachment placeholder block
        after the first). The body comes back verbatim, not stripped. ""
        for a row on a thread that is not operator or person chat (the
        chat server recalls only for those) and for a prompt no row
        matches. Runs on the loop thread, the only writer of `_sent`."""
        prompt = (prompt or "").strip()
        sent = [(row, text.strip()) for row, text in self._sent]
        match = next((row for row, text in reversed(sent) if text == prompt), None)
        if match is None:
            prefixed = [(len(text), row) for row, text in sent if text and prompt.startswith(text)]
            match = max(prefixed, key=lambda pair: pair[0])[1] if prefixed else None
        if match is None:
            return ""
        try:
            kind, _ = parse_thread(match["thread_id"])
        except DeliveryError:
            return ""
        return (match.get("body") or "") if kind in FOLDED_KINDS else ""

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
        self.turn.end()
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
        """Ask the loop to interrupt the turn running NOW. The lock only
        proves that turn was `running` when the request was queued; the
        coroutine runs at the loop's next yield, which can be after that
        turn ended and the next one started. So the request carries the
        turn's sequence number and `_interrupt_turn` checks it on the loop
        thread before touching the client: a stale request is dropped, it
        never lands on a later turn. A loop that closed anyway gets the
        coroutine closed here, never left unawaited."""
        with self._lock:
            if self.machine.state not in LIVE_STATES or self._loop is None \
                    or self._client is None:
                return False
            coro = self._interrupt_turn(self._turn_seq)
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:  # the loop closed under us
                coro.close()
                return False
        fut.add_done_callback(self._interrupt_done)
        return True

    async def _interrupt_turn(self, seq):
        """Runs on the loop thread, where turns start and end, so the check
        below and the interrupt cannot be split by a turn boundary. `_live`
        is cleared at every ResultMessage: an interrupt that arrives after
        the result, while the CLI is between turns, is dropped too, so it
        never marks a finished turn interrupted or reaches an idle CLI."""
        if seq != self._turn_seq or self.machine.state not in LIVE_STATES or not self._live:
            self.stream.append("system", {"subtype": "interrupt_dropped",
                                          "turn": seq, "current": self._turn_seq})
            return
        self._interrupt_requested = True
        await self._client.interrupt()

    def _interrupt_done(self, fut):
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            self.stream.append("error", {"error": "interrupt: %s: %s"
                                         % (type(exc).__name__, exc)})

    def rollover(self, reason):
        """End this generation and start the next (spec). One `flip` row
        (coalesced), claimed at the next turn boundary; waits for it. A
        runner that is not running leaves the durable row and says so."""
        return rollover.request(self.inbox, self.home, reason, alive=self.worker_alive,
                                timeout=self.handoff_deadline_s + rollover.WAIT_SLACK_S)

    def _request_rollover(self, why):
        """Ask without waiting (pressure, PreCompact): the loop claims it next."""
        inbox_id, coalesced = rollover.put_once(self.inbox, self.home, why)
        self.stream.append("rollover", {"phase": "coalesced" if coalesced else "requested",
                                        "reason": why, "inbox_id": inbox_id})

    def events(self, after=None):
        return self.stream.tail(after=after)

    def unsupported(self):
        return []

    # -- phase-2 CLI conveniences, NOT in the Runner protocol ------------------
    def worker_alive(self):
        """True while the worker thread runs. `cousin-runner` uses it to
        exit 3 when the worker gave up (a fatal connect failure)."""
        return self._thread is not None and self._thread.is_alive()

    # -- the loop ------------------------------------------------------------
    def _run_loop(self):
        # asyncio.Runner's close cancels leftover tasks, finalizes async
        # generators and joins the default executor before closing the loop.
        with asyncio.Runner() as runner:
            self._loop = runner.get_loop()
            runner.run(self._main())

    async def _main(self):
        try:
            if not await self._connect():
                return
            with wake.listen(self.home, self._wake_error) as listener:
                while not self._stop.is_set() and self.fatal is None:
                    await self._backoff()
                    if self._stop.is_set():
                        break
                    try:
                        rows = self.inbox.claim(limit=1, claimant=self.session_id)
                    except Exception as exc:  # noqa: BLE001 - a store failure is never silence
                        self._fail_turn([], exc)
                        await asyncio.sleep(0.2)  # a wedged store must not spin the loop
                        continue
                    if not rows:
                        await asyncio.get_running_loop().run_in_executor(
                            None, listener.wait, self.poll_s)
                        continue
                    try:
                        if rows[0]["source"] == "flip":
                            ok = await self._rollover_row(rows[0])
                        else:
                            ok = await self._turn(rows[0])
                    except Exception as exc:  # noqa: BLE001 - the idle transition can still raise
                        # `[]`: its rows are already closed (FakeRunner._fail_turn)
                        self._fail_turn([], exc)
                        ok = False
                    self._failures = 0 if ok else self._failures + 1
        finally:
            await self._disconnect()

    def _wake_error(self, message):
        self.stream.append("error", {"error": message})

    async def _backoff(self):
        """After `backoff_after` consecutive failed turns, wait before the
        next claim, once per failure: a broken lane must not burn the
        inbox one row at a time."""
        if self._failures < self.backoff_after or self._backed_off == self._failures:
            return
        self._backed_off = self._failures
        seconds = min(self.backoff_base_s * 2 ** (self._failures - self.backoff_after),
                      self.backoff_cap_s)
        self.stream.append("system", {"subtype": "backoff", "seconds": seconds,
                                      "failures": self._failures})
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    async def _connect(self, *, resume=None, why=None, fatal=True, event="connect_failed"):
        """A new client, connected. On failure the runner gives up: the
        machine goes `errored`, the reason is `self.fatal` and an `error`
        event, and the worker ends so a supervisor can restart it
        (`cousin-runner` exits 3). `why` is set on a reconnect. With
        `fatal=False` a failure is a `system` event of subtype `event`, no
        client, and False: the caller decides (the rollover's own path)."""
        try:
            self._client = self.client_factory(self.options(resume=resume))
            await self._client.connect()
            self._client_id = uuid.uuid4().hex
            return True
        except Exception as exc:  # noqa: BLE001 - a runner that cannot connect says so
            message = "%s: %s" % (type(exc).__name__, exc)
            if not fatal:
                self.stream.append("system", {"subtype": event, "session_id": resume,
                                              "error": message})
                self._client = None
                return False
            if why is not None:
                message = "reconnect failed: %s (after %s)" % (message, why)
            self._fail_connect(message, resume)
            return False

    async def _disconnect(self):
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001 - a dying client must not mask the stop
            pass

    def _fail_connect(self, message, resume=None):
        # The state first: whoever sees the `error` event also sees `errored`.
        self.fatal = message
        with self._lock:
            if self.machine.state in ("idle", "running"):
                self.machine.to("errored", message)
        self.stream.append("error", {"error": message, "fatal": True, "resumed": resume})

    def _row_item(self, row):
        return Item(thread_id=row["thread_id"], source=row["source"], body=row["body"],
                    sender=row["sender"], attachments=tuple(row["attachments"]),
                    context=row["context"], message_id=row["message_id"])

    async def _send(self, sdk, row, open_rows):
        """Write one row into the client. On success the row joins
        `open_rows` with the exact text its echo will carry. A query()
        that raised before anything was written raises `_NotWritten`."""
        message = envelope.render_message(self._row_item(row))
        # The SDK's str path sets this key and its iterable path does not.
        message.setdefault("parent_tool_use_id", None)
        text = message["message"]["content"][0]["text"]
        self._sent.append((row, text))   # before the query: the prompt hook may fire first
        yielded = []

        async def one():
            # `query` takes `str | AsyncIterable[dict]`; a bare dict would
            # reach its `async for` and raise TypeError.
            yielded.append(True)
            yield message

        gen = one()
        try:
            await self._client.query(gen)
        except Exception as exc:
            if not yielded or _nothing_written(sdk, exc):
                raise _NotWritten(row, exc) from exc
            open_rows.append((row, text))   # it may have reached the CLI
            raise
        finally:
            await gen.aclose()
        open_rows.append((row, text))

    async def _fold(self, sdk, open_rows):
        """Operator/person chat that arrived during the live turn is
        written into it (finding 1); anything else goes back to the queue."""
        rows = self.inbox.claim(limit=10, claimant=self.session_id)
        for i, row in enumerate(rows):
            if not folds_into_turn(row["source"], row["thread_id"]):
                self.inbox.requeue(row["id"])
                continue
            try:
                await self._send(sdk, row, open_rows)
            except Exception:
                for rest in rows[i + 1:]:   # claimed here, never offered: back to the queue
                    self.inbox.requeue(rest["id"])
                raise

    async def _next(self, it, started, fold):
        """The next message, or `_END` when the stream stops. The idle
        timeout bounds the wait for THIS message (a stream gone silent);
        the optional turn timeout bounds the whole turn. While waiting,
        `fold` (None once folding is over) runs every `poll_s`, so a row
        that lands during a long generation is written when it lands, not
        when the next message happens to arrive."""
        task = asyncio.ensure_future(it.__anext__())
        idle_deadline = time.monotonic() + self.idle_timeout_s
        try:
            while True:
                if fold is not None and time.monotonic() - self._last_fold >= self.poll_s:
                    self._last_fold = time.monotonic()
                    await fold()
                now = time.monotonic()
                wait, overrun = idle_deadline - now, "no message for %.1fs" % self.idle_timeout_s
                if self.turn_timeout_s is not None:
                    left = started + self.turn_timeout_s - now
                    if left < wait:
                        wait, overrun = left, "turn exceeded %.1fs" % self.turn_timeout_s
                if wait <= 0:
                    raise RunnerError(overrun)
                if fold is not None:
                    wait = min(wait, self.poll_s)
                done, _ = await asyncio.wait({task}, timeout=wait)
                if done:
                    try:
                        return task.result()
                    except StopAsyncIteration:
                        return _END
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001 - the read we abandoned, not ours to raise
                    pass

    def _match_echo(self, sdk, msg, open_rows, echoed):
        """The open row this UserMessage echoes, or None."""
        if isinstance(msg.content, str):
            texts = [msg.content]
        else:
            texts = [b.text for b in msg.content if isinstance(b, sdk.TextBlock)]
        for row, text in open_rows:
            if row["id"] not in echoed and text in texts:
                return row
        return None

    def _fail_turn(self, consumed, exc, *, recover=True, requeued=()):
        """FakeRunner._fail_turn's rules: `consumed` is only rows this call
        may safely close; `errored` only from idle/running and `errored ->
        idle` only from errored, so a machine `stop()` forced to `stopped`
        is never touched. `requeued` rows never reached the model and go
        back to the queue. A failure while closing rows is recorded and
        does not escape: the caller's resync must still run."""
        message = "%s: %s" % (type(exc).__name__, exc)
        # The state first: whoever sees the `error` event also sees `errored`.
        with self._lock:
            if self.machine.state in ("idle",) + LIVE_STATES:
                self.machine.to("errored", message)
        self.turn.end()
        self.stream.append("error", {"error": message})
        try:
            for row in consumed:
                self.inbox.done(row["id"], FAILED, message)
            for row in requeued:
                self.inbox.requeue(row["id"])
            self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                          "requeued": [r["id"] for r in requeued],
                                          "interrupted": self._interrupt_requested,
                                          "is_error": True, "num_turns": 0,
                                          "total_cost_usd": None, "session_id": None,
                                          "usage": None})
        except Exception as close_exc:  # noqa: BLE001 - recorded; the resync still runs
            self.stream.append("error", {"error": "closing a failed turn: %s: %s"
                                         % (type(close_exc).__name__, close_exc)})
        if recover:
            self._recover()

    def _recover(self):
        if self._stop.is_set() or self.fatal is not None:
            return
        with self._lock:
            if self.machine.state == "errored":
                self.machine.to("idle", "recovered")

    async def _resync(self):
        """After a turn failed mid-stream, the client's one message stream
        still holds the rest of that turn; left there, the next
        `receive_response()` stops at the OLD result and every later turn
        is off by one. Drain first (session continuity is the point);
        reconnect, resuming the last session seen, only if that fails. A
        runner being stopped does neither: the loop is about to close the
        client, and a new one would outlive the stop."""
        if self._stop.is_set():
            self.stream.append("system", {"subtype": "resync_skipped", "why": "stopping"})
            return
        try:
            drained = await self._drain()
        except Exception as exc:  # noqa: BLE001 - any drain failure means reconnect
            why = "%s: %s" % (type(exc).__name__, exc)
        else:
            self.stream.append("system", {"subtype": "drained", "messages": drained})
            return
        if self._stop.is_set():
            return
        resume = self._resume_id
        await self._disconnect()
        if await self._connect(resume=resume, why=why):
            self.stream.append("error", {"error": "reconnected: %s" % why, "resumed": resume})

    async def _drain(self):
        """Interrupt, then read the stream up to the failed turn's result,
        every wait bounded by `drain_timeout_s`. The result is recorded
        (`drained: True`): the failed turn's cost is not lost."""
        sdk = _sdk()
        deadline = time.monotonic() + self.drain_timeout_s
        overrun = "drain exceeded %.1fs" % self.drain_timeout_s
        try:
            await asyncio.wait_for(self._client.interrupt(),
                                   timeout=deadline - time.monotonic())
        except asyncio.TimeoutError:
            raise RunnerError(overrun) from None
        responses = self._client.receive_response()
        it = responses.__aiter__()
        count = 0
        try:
            while True:
                msg = await _next_by(it, deadline, overrun)
                if msg is _END:
                    raise RunnerError("stream ended without a result")
                count += 1
                self._record(sdk, msg)
                if isinstance(msg, sdk.ResultMessage):
                    self.stream.append("result", {"inbox_ids": [], "drained": True,
                                                  "interrupted": True,
                                                  "is_error": bool(msg.is_error),
                                                  "num_turns": msg.num_turns,
                                                  "total_cost_usd": msg.total_cost_usd,
                                                  "session_id": msg.session_id,
                                                  "usage": msg.usage})
                    return count
        finally:
            await _aclose(responses)

    async def _turn(self, first):
        """One runner turn: write `first`, fold operator/person chat while
        the turn is live, and read until every written row is closed.
        Returns False when the turn failed or a result was an error."""
        self._interrupt_requested = False
        self._sent = []
        open_rows = []    # (row, envelope text): written, not yet closed
        closing = []      # rows a result is closing right now
        ok = True
        try:
            sdk = _sdk()
            with self._lock:
                self._turn_seq += 1
                self._live = True
                self.machine.to("running", "turn")
            self.turn.begin(first)
            self.stream.append("turn_start", {"inbox_ids": [first["id"]],
                                              "bodies": [first["body"]],
                                              "thread_id": first["thread_id"]})
            await self._send(sdk, first, open_rows)
            started = time.monotonic()
            self._last_fold = 0.0
            results = 0

            async def fold():
                if self._live and not self._interrupt_requested and results == 0:
                    await self._fold(sdk, open_rows)

            while open_rows:
                echoed = set()
                responses = self._client.receive_response()
                it = responses.__aiter__()
                try:
                    while True:
                        msg = await self._next(it, started, fold if results == 0 else None)
                        if msg is _END:
                            # the CLI died: the SDK ends the stream on {"type": "end"}
                            raise RunnerError("stream ended without a result")
                        echo_of = None
                        if isinstance(msg, sdk.UserMessage):
                            row = self._match_echo(sdk, msg, open_rows, echoed)
                            if row is not None:
                                echo_of = row["id"]
                                echoed.add(echo_of)
                                self._live = True   # the CLI took up a carried row
                                if row["id"] != first["id"]:
                                    self.turn.add(row)
                        # a message means the permission was settled; a hook's
                        # own event is not one
                        if not isinstance(msg, getattr(sdk, "HookEventMessage", ())):
                            with self._lock:
                                if self.machine.state == "waiting_permission":
                                    self.machine.to("running", "message")
                        self._record(sdk, msg, echo_of=echo_of)
                        if isinstance(msg, sdk.ResultMessage):
                            results += 1
                            self._live = False
                            ok = self._close(msg, open_rows, echoed, closing) and ok
                            await self._after_turn(msg)
                            break
                finally:
                    await _aclose(responses)
        except Exception as exc:  # noqa: BLE001 - a raising turn body is recorded, not lost
            requeued = [exc.row] if isinstance(exc, _NotWritten) else []
            cause = exc.cause if isinstance(exc, _NotWritten) else exc
            unclosed = [row for row, _ in open_rows] + closing
            try:
                self._fail_turn(unclosed, cause, recover=False, requeued=requeued)
            finally:
                self._live = False
                if open_rows or requeued:
                    # the stream may hold this turn's messages, or the client is broken
                    await self._resync()
                self._recover()
            return False

        self.turn.end()
        with self._lock:
            if self.machine.state in LIVE_STATES:
                self.machine.to("idle", "turn done")
        return ok

    def _close(self, msg, open_rows, echoed, closing):
        """Close every row echoed since the last result with this one;
        rows written but not echoed stay open for the next CLI turn."""
        interrupted = self._interrupt_requested
        is_error = bool(msg.is_error)
        closing[:] = [row for row, _ in open_rows if row["id"] in echoed]
        open_rows[:] = [(row, text) for row, text in open_rows if row["id"] not in echoed]
        outcome = FAILED if (is_error and not interrupted) else DELIVERED
        ids = [row["id"] for row in closing]
        while closing:
            self.inbox.done(closing[0]["id"], outcome,
                            "turn %s" % (msg.session_id or self.session_id))
            closing.pop(0)
        self.stream.append("result", {"inbox_ids": ids, "interrupted": interrupted,
                                      "is_error": is_error, "num_turns": msg.num_turns,
                                      "total_cost_usd": msg.total_cost_usd,
                                      "session_id": msg.session_id, "usage": msg.usage})
        self._interrupt_requested = False
        return not (is_error and not interrupted)

    async def _after_turn(self, msg):
        """The single hook for a ResultMessage's post-close work, called
        from `_turn` once per result, right after `_close`: the usage
        record, then extraction of the session's new transcript entries
        (the SDK flushed the store before it yielded the result, so the
        whole turn is there). Each step runs off the loop (blocking sqlite
        and file work) and never raises into it: a failure is a `usage`
        or `extract` event, never a broken turn, and a failed usage record
        does not stop the extraction. Last, the context pressure check
        (rollover.pressure_due, held back by the hysteresis): a rollover
        is requested, never run here; the loop claims it at the boundary."""
        await self._record_usage(msg)
        await self._mine(self._resume_id)
        try:
            usage_now = await self._context_usage()
            armed = self.hysteresis.allow(usage_now, self.rollover_at_percent)
            if armed and rollover.pressure_due(usage_now, self.rollover_at_percent):
                self._request_rollover(_pressure_reason(usage_now))
        except Exception as exc:  # noqa: BLE001 - the trigger must never fail a turn
            self.stream.append("error", {"error": "rollover trigger: %s: %s"
                                         % (type(exc).__name__, exc)})

    async def _record_usage(self, msg):
        """The usage record of one ResultMessage, off the loop; a failure is
        a `usage` event with the error, never a failed turn."""
        try:
            row = await asyncio.to_thread(
                usage.record, self.home, client_id=self._client_id, session_id=msg.session_id,
                result={"usage": msg.usage, "total_cost_usd": msg.total_cost_usd,
                        "session_id": msg.session_id}, lane=self._lane)
        except Exception as exc:  # noqa: BLE001 - usage must never fail a turn
            self.stream.append("usage", {"error": "%s: %s" % (type(exc).__name__, exc)})
        else:
            self.stream.append("usage", {k: row[k] for k in ("cost_usd", "estimate", "total",
                                                             "error") if k in row})

    async def _mine(self, sid, **extra):
        """Mine the session's new transcript entries into raw memory, off
        the loop; the outcome is an `extract` event, never a raise."""
        if not sid:
            return      # no session named yet: nothing to mine
        payload = dict({"session_id": sid, "turn": self._turn_seq}, **extra)
        store = getattr(self, "session_store", None)
        if store is None:
            # visible in the stream, never a silent stop of extraction
            self.stream.append("extract", dict(payload, written=0, skipped="no session store"))
            return
        try:
            payload["written"] = await asyncio.to_thread(
                extract.mine_turn, self.home, sid, self._turn_seq, store=store)
        except Exception as exc:  # noqa: BLE001 - extraction must never fail a turn
            payload.update(written=-1, error="%s: %s" % (type(exc).__name__, exc))
        self.stream.append("extract", payload)

    async def _context_usage(self):
        """The client's context usage, or None when it cannot say."""
        try:
            return await asyncio.wait_for(self._client.get_context_usage(), 5.0)
        except Exception:  # noqa: BLE001 - no reading is no trigger, never a failed turn
            return None

    # -- the rollover (rollover.py) ------------------------------------------
    async def _until_stopped(self):
        while not self._stop.is_set():
            await asyncio.sleep(0.1)

    async def _handoff_exchange(self, sdk, reason):
        """One turn on the dying session asking for the handoff: write the
        request, read to its result. Returns the tool's summary, or None
        when the model answered without calling it. The request is not an
        inbox row: the durable row is the `flip` row itself."""
        row = {"id": -1, "thread_id": "system", "source": "flip", "sender": "runner",
               "body": rollover.handoff_request_text(reason), "attachments": [],
               "context": "", "message_id": None}
        self._sent = []
        await self._send(sdk, row, [])
        responses = self._client.receive_response()
        it = responses.__aiter__()
        try:
            while True:
                msg = await _next_by(it, time.monotonic() + self.handoff_deadline_s,
                                     "handoff deadline")
                if msg is _END:
                    raise RunnerError("stream ended during the handoff")
                self._record(sdk, msg)
                if isinstance(msg, sdk.ResultMessage):
                    await self._record_usage(msg)      # the handoff turn costs too
                    break
        finally:
            await _aclose(responses)
        return self.handoff_box.summary

    async def _ask_handoff(self, reason):
        """'clean' when the handoff tool answered in time; 'emergency' when
        it did not (the file is then written from the store's tail);
        'stopped' when the runner was stopped while waiting."""
        sdk = _sdk()
        self.handoff_box.arm(asyncio.get_running_loop())
        exchange = asyncio.ensure_future(self._handoff_exchange(sdk, reason))
        stopper = asyncio.ensure_future(self._until_stopped())
        try:
            done, _ = await asyncio.wait({exchange, stopper}, timeout=self.handoff_deadline_s,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (exchange, stopper):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - the abandoned wait, not ours to raise
                        pass
            self.handoff_box.disarm()
        failure = None
        if exchange in done and not exchange.cancelled():
            failure = exchange.exception()   # read on every path: never "never retrieved"
        if self._stop.is_set():
            return "stopped"
        if self.handoff_box.summary is not None:
            self.stream.append("rollover", {"phase": "handoff", "handoff": "clean"})
            return "clean"
        if exchange not in done:
            why = "handoff timeout (%.0fs)" % self.handoff_deadline_s
        elif failure is not None:
            why = "handoff turn failed: %s: %s" % (type(failure).__name__, failure)
        else:
            why = "the model finished its turn without calling handoff"
        try:
            await asyncio.wait_for(self._client.interrupt(), 5.0)
        except BaseException:  # noqa: BLE001 - a dying client; the file is what matters
            pass
        tail = await asyncio.to_thread(self.session_store.tail_text, self._resume_id or "", 2000)
        await asyncio.to_thread(rollover.write_emergency_handoff, self.home,
                                name=self.tool_context.name, reason=why, tail=tail)
        self.stream.append("rollover", {"phase": "handoff", "handoff": "emergency", "why": why})
        return "emergency"

    def _close_duplicates(self, row, outcome, detail):
        """A PLAIN duplicate `flip` row that slipped past put_once (two
        processes between check and put) gets this rollover's answer, not a
        second rollover. A bequest row is never closed here (R10): it is an
        operator act and runs as its own rollover. The close is guarded on
        the body read here: a bequest that replaced it since is left open."""
        for other in self.inbox.open_rows("flip"):
            if other["id"] != row["id"] and other["state"] == "queued" \
                    and not rollover.is_bequest(other["body"]) \
                    and self.inbox.done_if_queued(other["id"], outcome,
                                                  json.dumps(dict(detail, coalesced_into=row["id"])),
                                                  body=other["body"]):
                self.stream.append("rollover", {"phase": "coalesced", "inbox_id": other["id"],
                                                "into": row["id"]})

    async def _rollover_row(self, row):
        """The rollover, at a turn boundary: handoff, end hooks, archive,
        a final mine, a new client with no resume, THEN the generation,
        start hooks, the digest as the first message.

        Before the new session exists, any failure is errored -> idle with
        the row failed, the old session kept (a client resumed on it, or at
        least `_resume_id` naming it for the next reconnect), and the detail
        saying where it stopped: never a wedged machine (C1). Once the new
        session exists (the point of no return) nothing fails the rollover:
        a failed step degrades it, is named in the row's detail, and the row
        still closes delivered, because running it again would move the
        generation twice for one request."""
        reason = row["body"] or "rollover"
        old_sid = self._resume_id
        disconnected = False
        generation = boot.read_generation(self.home)
        with self._lock:
            if self.machine.state != "idle":     # stop() won the race: the row waits
                self.inbox.requeue(row["id"])
                return False
            self.machine.to("rolling_over", reason.splitlines()[0][:120])
        self.stream.append("rollover", {"phase": "start", "reason": reason, "session_id": old_sid})
        try:
            handoff = await self._ask_handoff(reason)
            if handoff == "stopped":
                self.inbox.requeue(row["id"])          # the next start finishes this rollover
                self.stream.append("rollover", {"phase": "requeued", "reason": reason})
                return False
            await asyncio.to_thread(session.run_phase, self.home, "end")
            await asyncio.to_thread(rollover.archive_generation, self.home, generation)
            await self._disconnect()
            disconnected = True
            # a final mine: nothing of the old session arrives after this
            await self._mine(old_sid, final=True)
            self._resume_id = None
            if not await self._connect(resume=None, why="rollover", fatal=False):
                raise RunnerError("could not start the new session")
            disconnected = False
        except Exception as exc:  # noqa: BLE001 - a failed rollover must not wedge the runner
            message = "%s: %s" % (type(exc).__name__, exc)
            self.hysteresis.rolled_over()      # no re-request every turn over the threshold
            # No new session was established: the old one is still the session,
            # so a later reconnect (this fallback, or _resync) resumes it and
            # never starts an unbumped, digest-less fresh one.
            if self._resume_id is None:
                self._resume_id = old_sid
            with self._lock:
                if self.machine.state == "rolling_over":
                    self.machine.to("errored", "rollover failed: " + message)
            if disconnected and not self._stop.is_set():
                await self._connect(resume=old_sid, why="rollover failed", fatal=False)
            detail = {"reason": reason, "error": message,
                      "generation": boot.read_generation(self.home),
                      "old_session": old_sid, "new_session": None}
            self.inbox.done(row["id"], FAILED, json.dumps(detail))
            self._close_duplicates(row, FAILED, detail)
            self.stream.append("rollover", dict(detail, phase="failed"))
            self._recover()
            return False
        # The point of no return: a new session exists, so the generation moves.
        problems = []
        try:
            generation = boot.bump_generation(self.home)
        except Exception as exc:  # noqa: BLE001 - degraded, named in the detail
            problems.append("generation not moved: %s: %s" % (type(exc).__name__, exc))
        self.hysteresis.rolled_over()
        try:
            await asyncio.to_thread(session.run_phase, self.home, "start")
        except Exception as exc:  # noqa: BLE001 - degraded, named in the detail
            problems.append("start hooks: %s: %s" % (type(exc).__name__, exc))
        digest, digest_state = await self._digest(generation)
        digest_id = None
        if digest is not None:
            try:
                digest_id = self.inbox.put(Item(thread_id="system", source="boot", body=digest,
                                                sender="runner"))
            except Exception as exc:  # noqa: BLE001 - the session runs on without it
                digest_state = "none: the digest row could not be stored: %s: %s" \
                               % (type(exc).__name__, exc)
        detail = {"reason": reason, "handoff": handoff, "generation": generation,
                  "old_session": old_sid, "digest": digest_state}
        if problems:
            detail["problems"] = problems
        with self._lock:
            # stop() may have forced `stopped` meanwhile: the row closes all the same
            if self.machine.state == "rolling_over":
                self.machine.to("idle", "rolled over")
        self.inbox.done(row["id"], DELIVERED, json.dumps(detail))
        self._close_duplicates(row, DELIVERED, detail)
        self.stream.append("rollover", dict(detail, phase="done"))
        if self._stop.is_set() or digest_id is None:
            return True     # a stored digest row stays queued (durable): the next start runs it
        # The digest is the new session's FIRST message: claimed by id and run
        # now, ahead of chat that queued up during the rollover (same priority,
        # older). The row is durable, so a crash here replays it at the next start.
        first = self.inbox.claim_id(digest_id, claimant=self.session_id)
        if first is not None:
            return await self._turn(first)
        return True

    async def _digest(self, generation):
        """(text or None, state): the state digest ("built"); the last
        handoff marked degraded when it cannot be built ("degraded: ...");
        None only when even that failed ("none: ..."). Never raises."""
        from cousin_lib.runner import prompt
        try:
            return ((await asyncio.to_thread(prompt.state_digest, self.home, root=self.root,
                                             slug=self.tool_context.slug,
                                             generation=generation))["text"], "built")
        except Exception as exc:  # noqa: BLE001 - a degraded digest, never none
            state = "degraded: %s: %s" % (type(exc).__name__, exc)
        try:
            return rollover.degraded_digest(self.home, slug=self.tool_context.slug,
                                            generation=generation, error=state), state
        except Exception as exc:  # noqa: BLE001 - the new session runs on without one
            return None, "none: %s; then %s: %s" % (state, type(exc).__name__, exc)

    def _record(self, sdk, msg, *, echo_of=None):
        """Every SDK message leaves at least one event (a ResultMessage's
        is its caller's `result`)."""
        if isinstance(msg, sdk.SystemMessage):
            if msg.subtype == "init":
                d = msg.data or {}
                self._resume_id = d.get("session_id") or self._resume_id
                self._lane = usage.lane_for(d.get("apiKeySource"))
                self.stream.append("session_init", {"apiKeySource": d.get("apiKeySource"),
                                                    "model": d.get("model"),
                                                    "session_id": d.get("session_id"),
                                                    "tools": list(d.get("tools") or []),
                                                    "mcp_servers": list(d.get("mcp_servers") or [])})
            elif msg.subtype == "mirror_error":
                # the SDK retried and dropped this batch: the store is missing entries
                self.stream.append("error", {"error": "session store append failed: %s"
                                             % (getattr(msg, "error", "") or msg.data),
                                             "mirror_error": True})
            else:
                self.stream.append("system", {"subtype": msg.subtype})
        elif isinstance(msg, sdk.AssistantMessage):
            recorded = 0
            for block in msg.content:
                if isinstance(block, sdk.TextBlock):
                    self.stream.append("text", {"text": block.text})
                elif isinstance(block, sdk.ToolUseBlock):
                    self.stream.append("tool", {"id": block.id, "name": block.name,
                                                "input": block.input})
                elif isinstance(block, sdk.ThinkingBlock):
                    self.stream.append("thinking", {"length": len(block.thinking or "")})
                else:
                    continue
                recorded += 1
            if not recorded:
                self.stream.append("text", {"text": ""})
        elif isinstance(msg, sdk.UserMessage):
            if isinstance(msg.content, str):
                self.stream.append("user", {"text": msg.content[:2000], "echo_of": echo_of})
                return
            texts, recorded = [], 0
            for block in msg.content:
                if isinstance(block, sdk.ToolResultBlock):
                    self.stream.append("tool_result", {
                        "tool_use_id": block.tool_use_id, "is_error": bool(block.is_error),
                        "text": _tool_result_text(block.content)[:2000]})
                    recorded += 1
                elif isinstance(block, sdk.TextBlock):
                    texts.append(block.text)
            if texts or not recorded:
                self.stream.append("user", {"text": "\n".join(texts)[:2000],
                                            "echo_of": echo_of})
        elif isinstance(msg, sdk.ResultMessage):
            # the caller closes the turn from it and appends its `result`
            self._resume_id = msg.session_id or self._resume_id
        else:
            self.stream.append("other", {"type": type(msg).__name__})
