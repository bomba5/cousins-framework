"""SdkRunner: a cousin on the Claude Agent SDK, no terminal.

The only module in the framework that imports claude_agent_sdk, and it
does so lazily so the core stays importable without the extra. The
client lives for the runner's life (it survives a ten-minute idle on
both auth lanes). The credentials are the cousin's
account (accounts.py), rendered into `options.env` and nothing else;
`apiKeySource` from every init message goes to the event stream so a
cousin whose account did not take effect is visible.

One thread owns one asyncio loop, and that loop owns the client. The
loop's shape is FakeRunner's (the reference runner): claim one row, run
one turn, fold operator/person/peer chat that lands mid-turn into it, close
every consumed row with a result, and route every failure through
`_fail_turn` so nothing dies silently.

A turn is not 1:1 with a CLI turn. The CLI is
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
import collections
import json
import os
import threading
import contextlib
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import accounts, boot, handover, review_gate, session, usage
from cousin_lib.delivery import DELIVERED, FAILED, QUEUED, DeliveryError, Item, parse_thread
from cousin_lib.runner import (auth, config_watch, cost_cap, envelope, extract, hooks,
                               memory_watch, restart_note, rollover, tool_ledger, tools, wake)
from cousin_lib.runner.base import (INTERRUPT, NO_TURN, SURFACE_KINDS, Receipt, RunnerError,
                                     folds_into_turn)
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
# _next's answers when the turn stops waiting on a carried row: a stop
# came (after its grace), or an interrupt's bound passed with no echo
_CARRY_STOPPED = object()
_CARRY_DROPPED = object()
# How long a stop still waits for a carried row's echo; stop()'s own
# interrupt can be what makes the CLI take the row (capped by drain_timeout_s)
CARRY_STOP_GRACE_S = 2.0
# A turn is live while the model runs it, including while it waits on a
# permission: every exit from either state (errored, idle, stopped) is legal.
LIVE_STATES = ("running", "waiting_permission")
# One memory system (spec): the agent CLI keeps its own auto-memory
# unless told not to, and on this lane framework memory is the only one.
# The bundled CLI reads this variable (and the setting autoMemoryEnabled:
# false) and logs which one disabled it. Set after the account's env so
# no account kind can drop it; proven by effect in
# tests/runner/test_live_prompt.py, with a control run.
AUTO_MEMORY_OFF = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
# config.commit_attribution decides whether the CLI's own
# injected attribution (a Co-Authored-By trailer, a "Generated with
# Claude Code" line) reaches a commit or PR this cousin makes. False
# composes into options.settings, the CLI's --settings (the highest-
# priority user-controlled layer per claude_agent_sdk's ClaudeAgentOptions
# docstring); True passes no `settings` at all, so nothing here overrides
# the CLI's stock behaviour.
ATTRIBUTION_OFF_SETTINGS = json.dumps({"includeCoAuthoredBy": False,
                                       "attribution": {"commit": "", "pr": ""}})


def _attribution_settings(commit_attribution):
    return None if commit_attribution else ATTRIBUTION_OFF_SETTINGS
# The session a runner is (runner/sessions.py): the primary holds
# the generation; a side session is named by the thread kind it answers.
PRIMARY = "primary"
# The home's and the plugins' MCP servers reach the CLI as a file
# (_mcp_options): the SDK writes `mcp_servers` inline on the CLI's argv,
# which every local user can read. The CLI merges every --mcp-config it is
# given (measured on 2.1.281).
MCP_CONFIG_FLAG = "mcp-config"                     # the CLI's flag, without its dashes
MCP_CONFIG_FILE = ("data", "run", "mcp-config.json")


class _NotWritten(Exception):
    """query() raised before the row reached the transport: the row is
    requeued, never failed, because the model never saw it."""

    def __init__(self, row, cause):
        super().__init__(str(cause))
        self.row, self.cause = row, cause


class _Unrenderable(Exception):
    """The row could not be rendered into a message (a broken attachment,
    say). Not transient: the row is closed FAILED with this text, never
    requeued to fail the same way again."""

    def __init__(self, row, cause):
        super().__init__("could not be rendered: %s: %s" % (type(cause).__name__, cause))
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


class _Job:
    """One write the turn's writer runs: `fn` is awaited on the writer
    task. `on_ok(value)` and `on_error(exc)` run right after `fn` returns
    or raises; `on_dropped(started)` runs when the writer closes first:
    `started` True for the write it cut off mid-flight (it may have
    reached the CLI), False for one never begun. All on the loop thread."""

    def __init__(self, fn, on_ok=None, on_error=None, on_dropped=None):
        self.fn, self.on_ok, self.on_error, self.on_dropped = fn, on_ok, on_error, on_dropped
        self.started = False
        self.future = asyncio.get_running_loop().create_future()


class _Writer:
    """The one task that writes into the client while a turn runs. The
    turn's reader must never await a write: once the
    CLI's stdout is full and unread (the SDK's 100-message buffer, then
    the pipe), the CLI stops reading its stdin, the write blocks, the
    reader waiting on it never drains the stdout, and every hook reply
    queued behind the transport's one write lock times out: measured
    stalls of 18 to 39 minutes. So a mid-turn write (a fold, an interrupt) is
    handed to this task and the reader goes on reading. One task, one
    FIFO queue: writes reach the CLI in the order they were handed over,
    so a fold taken before an interrupt is written before it. `close()`
    cancels a write in progress and drops the ones never begun, each
    job's `on_dropped` called before it returns; no task outlives it."""

    def __init__(self, report):
        self._report = report       # report(text): said on the stream, never fatal
        self._jobs = collections.deque()
        self._wake = asyncio.Event()
        self._current = None        # the job being written
        self._closed = False
        self._task = asyncio.ensure_future(self._run())

    def ended(self):
        """Why the writer's task ended without a close, or None."""
        if self._closed or not self._task.done():
            return None
        if self._task.cancelled():
            return "the turn's writer ended: cancelled"
        exc = self._task.exception()
        return "the turn's writer ended: %s" % (
            "%s: %s" % (type(exc).__name__, exc) if exc is not None else "returned")

    def submit(self, job):
        if self._closed:
            raise RunnerError("the turn's writer is closed")
        if self._task.done():       # died without a close: nothing would ever write it
            raise RunnerError("the turn's writer has ended")
        self._jobs.append(job)
        self._wake.set()
        return job

    async def _run(self):
        while True:
            if not self._jobs:
                self._wake.clear()
                await self._wake.wait()
                continue
            job = self._jobs.popleft()
            job.started = True
            self._current = job
            try:
                value = await job.fn()
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise       # close(): it drops this job as started
                # raised inside the client, not asked for: a failed write,
                # said, and the writer goes on with the next one
                exc = RunnerError("the write was cancelled inside the client")
                self._report("writer: %s" % exc)
                self._fail(job, exc)
            except Exception as exc:  # noqa: BLE001 - handed to the job's owner
                self._fail(job, exc)
            else:
                if not job.future.done():       # its awaiter may have cancelled it
                    job.future.set_result(value)
                self._call(job.on_ok, value)
            self._current = None

    def _fail(self, job, exc):
        if not job.future.done():       # its awaiter may have cancelled it
            job.future.set_exception(exc)
            job.future.exception()      # read here: its owner may not await it
        self._call(job.on_error, exc)

    def _call(self, handler, arg):
        # a handler that raises must not end the writer, nor the close: the
        # jobs behind it would never be written, or never be dropped
        if handler is None:
            return
        try:
            handler(arg)
        except Exception as exc:  # noqa: BLE001 - said, and the writer goes on
            self._report("writer: %s: %s" % (type(exc).__name__, exc))

    async def close(self):
        """Stop the writer: the write in progress is cancelled and dropped
        as started, every job never begun is dropped as not started, in
        order, before this returns. A cancellation of the task calling
        close() (the turn's) is re-raised once the jobs are dropped."""
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        cancelled = False
        try:
            await self._task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            cancelled = me is not None and me.cancelling() > 0
        except Exception as exc:  # noqa: BLE001 - the writer died: said, jobs still dropped
            self._report("writer ended: %s: %s" % (type(exc).__name__, exc))
        finally:
            dropped = []
            if self._current is not None:
                dropped.append((self._current, True))
                self._current = None
            while self._jobs:
                dropped.append((self._jobs.popleft(), False))
            for job, started in dropped:
                if not job.future.done():
                    job.future.cancel()
                self._call(job.on_dropped, started)
        if cancelled:
            raise asyncio.CancelledError()


def _pressure_reason(context_usage):
    """The rollover reason for a pressure reading: the percentage when the
    reading has one, else the token count that pulled the trigger."""
    try:
        return "context pressure %d%%" % int(context_usage["percentage"])
    except (KeyError, TypeError, ValueError):
        return "context pressure %s tokens" % (context_usage or {}).get("totalTokens")


def _default_factory(options):
    return _sdk().ClaudeSDKClient(options=options)


# A background task's lifecycle (the SDK's TaskStarted/Progress/
# Updated/NotificationMessage) on the stream, kept to what the pane's task
# list shows: never its prompt, output file or output.
TASK_SUBTYPES = ("task_started", "task_progress", "task_updated", "task_notification")
TASK_SUMMARY_CHARS = 300


def _task_str(value, cap=TASK_SUMMARY_CHARS):
    return (value.strip()[:cap] or None) if isinstance(value, str) else None


def _task_payload(subtype, data):
    """A task message's `system` payload from its raw data: the id and, per
    subtype, the description and type (started), the last tool and usage
    totals (progress), the status when the patch carries one (updated), the
    status and a bounded summary (notification)."""
    d = data or {}
    out = {"subtype": subtype, "task_id": _task_str(d.get("task_id"))}
    if subtype == "task_started":
        out.update(description=_task_str(d.get("description")),
                   task_type=_task_str(d.get("task_type")),
                   tool_use_id=_task_str(d.get("tool_use_id")))
    elif subtype == "task_progress":
        u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
        out.update(last_tool_name=_task_str(d.get("last_tool_name")),
                   usage={k: u[k] for k in ("total_tokens", "tool_uses", "duration_ms")
                          if isinstance(u.get(k), (int, float)) and not isinstance(u.get(k), bool)})
    elif subtype == "task_updated":
        patch = d.get("patch") if isinstance(d.get("patch"), dict) else {}
        status = _task_str(patch.get("status"))
        if status:
            out["status"] = status
    else:
        out.update(status=_task_str(d.get("status")), summary=_task_str(d.get("summary")))
    return out


# A thinking block in the reasoning stream: its text, bounded as a tool
# result's is (the whole block stays in the session store's transcript).
# the block helpers are shared with the tmux kind
from cousin_lib.runner.blocks import THINKING_CHARS  # noqa: E402,F401 - re-exported
from cousin_lib.runner.blocks import thinking_payload as _thinking_payload  # noqa: E402
from cousin_lib.runner.blocks import tool_result_text as _tool_result_text  # noqa: E402


# A turn's fold, interrupt-row control or query write that runs
# longer than this is named on the stream (`system` `stall`, its site and
# duration): the SDK buffers 100 messages from the CLI, and a consumer held
# that long lets it fill, after which its reader answers no hook.
STALL_REPORT_S = 30.0
STALL_CHECK_S = 5.0

class SdkRunner:
    kind = "sdk"          # what runner/status.py reports (the `runner` event)
    # The contract items this runner DECLARES unsupported, and those a
    # plugin meets; read at class level by runner/contract_table.py
    UNSUPPORTED = ()
    PLUGIN_ITEMS = ()
    # The console's interrupt row (thread `system`) targets the primary
    # session's live turn. A session class that must never take it
    # (SideSession) sets this False.
    takes_interrupts = True
    # Whether this session runs the review gate's start-up sweep: every held
    # entry a reviewer has tried fewer than MAX_ATTEMPTS times. One session
    # per home does, or each would review the same rows (SideSession
    # sets it False).
    sweeps_at_start = True
    # The primary takes the restart mark and says so to its resumed
    # session; a side session (SideSession) leaves it to the primary
    takes_restart_note = True
    # After this many consecutive failed turns the loop waits before its
    # next claim: backoff_base_s, doubling, capped; a good turn resets it.
    backoff_after = 3
    backoff_base_s = 1.0
    backoff_cap_s = 30.0
    # how often a runner waiting for a login reads the cheap signals (the
    # credential mark, the login file) between two full looks
    login_poll_s = 1.0
    # A login retry failing this many times in a row for another reason
    # gives up the session on file and starts fresh (_login_retry_failed).
    RETRY_FAILURES_TO_FRESH = 3
    # How often a live turn looks for operator/person/peer rows to fold in, and
    # how long an idle loop sleeps when the doorbell is a Poller.
    poll_s = 0.2
    # The idle bound while a tool call is open (its tool_use seen, its
    # tool_result not yet): a tool is silent while it runs, and a long one
    # is no stalled stream. The idle_timeout_s bounds every other wait.
    tool_idle_timeout_s = 3600.0

    def __init__(self, home, *, client_factory=None, account=None, api_key=None, model=None,
                 effort=None, cwd=None, idle_timeout_s=600.0, turn_timeout_s=None,
                 drain_timeout_s=30.0, policy=None, registry=None, handoff_deadline_s=None,
                 session=PRIMARY, claim_kinds=None, exclude_kinds=(), memory_reviewer=None,
                 commit_attribution=None):
        self.home = Path(home)
        # Which rows this session claims: claim_kinds None is every
        # kind but exclude_kinds; a side session names its own kind.
        self.session = session
        self.claim_kinds = None if claim_kinds is None else tuple(claim_kinds)
        self.exclude_kinds = tuple(exclude_kinds)
        # the tools and the model's own commands find the cousin and the
        # install through these; cousin-runner exports them, and a runner
        # built directly sets whichever is unset
        from cousin_lib.runner.main import export_environment
        export_environment(self.home, overwrite=False)
        # The account (accounts.py) is the only source of credentials:
        # cousin-runner passes the cousin's; `api_key` builds the implicit
        # key account (kept for callers and tests); with neither, the host's
        # login. Every runner has an account, and its KIND picks the resume
        # path (_resume_via_cli).
        if account is None:
            account = (accounts.Account(self.home.name, "anthropic-key", None, None,
                                        implicit=True, secret_value=api_key)
                       if api_key else accounts.Account(accounts.HOST, "claude-login", None, None,
                                                        implicit=True))
        self.account = account
        self.model = model
        # [agent] effort (runner_for checked the level): the CLI's --effort
        self.effort = effort
        self.cwd = Path(cwd) if cwd else self.home
        self.idle_timeout_s = float(idle_timeout_s)
        self.turn_timeout_s = None if turn_timeout_s is None else float(turn_timeout_s)
        self.drain_timeout_s = float(drain_timeout_s)
        self.client_factory = client_factory or _default_factory
        self.session_id = "sdk-%s%s" % ("" if session == PRIMARY else session + "-",
                                        uuid.uuid4().hex[:8])
        self._turn_started_at = None   # the live turn's start (activity())
        self.inbox = Inbox(self.home)
        self.stream = EventStream(self.home, self.session_id)
        self.machine = StateMachine(on_change=self._on_state)
        self.turn = Turn()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stopping = threading.Event()   # a stop was asked for: nothing new is claimed
        self._thread = None
        self._loop = None
        self._client = None
        self._interrupt_requested = False
        self._interrupt_sent = False    # an interrupt reached the client this turn (_close)
        self._live = False       # the CLI is generating for this turn (see _interrupt_turn)
        # The turn waits on a carried row (written, its echo not come when
        # the CLI's result did); interrupts are taken, and stop and an
        # interrupt bound the wait (_carried_wait)
        self._carrying = False
        self._carry_until = None
        self._carry_stop_at = None    # a stop's grace for the echo (CARRY_STOP_GRACE_S)
        # an interrupt written while the CLI was between turns: it may
        # have cut nothing, so the echo sends it again to the live turn;
        # cleared once an interrupt reaches a live turn
        self._interrupt_idle = False
        self._reinterrupting = False
        # The reader of the CLI's stream between turns (_pump_run),
        # and whether a CLI turn of its own (a task notification) is open
        self._pump = None
        self._pump_turn_open = False
        self._turn_seq = 0
        self._resume_id = None   # the last session_id an init or result named
        self._client_id = None   # a fresh uuid per connect (usage.record's client key)
        self._lane = "unknown"   # from the last session_init's apiKeySource
        self._failures = 0       # consecutive failed turns
        self._backed_off = 0     # the failure count the last backoff was for
        self._last_fold = 0.0    # when the live turn last looked for rows to fold
        self._open_tools = set()  # the live turn's tool_use ids with no tool_result yet
        self._waits = []         # the turn's long waits in progress
        # The live turn's writer (_Writer): every mid-turn write goes through
        # it, never awaited by the reader. None between turns.
        self._writer = None
        self._write_error = None  # a fold write that failed: the reader raises it
        self._unwritten = []      # folds whose write wrote nothing: requeued by the failure path
        self._drop_writes = False  # set by _close's login branch: close the writer at once
        # A rejected rate limit (epoch seconds): nothing is claimed before it
        # (_wait_rate_limit); None when no limit holds.
        self._limited_until = None
        self.fatal = None        # why the worker gave up (a connect failure), else None
        # A dying login (auth.py): the runner waits for the
        # operator's fix, claims nothing meanwhile, and never sets `fatal`.
        self._login_blocked = False   # waiting for a login (or billing): claim nothing
        self._login_attempt = 0
        self._login_mark = None       # auth.credential_mark at the failure
        self._login_was_in = False    # `claude auth status` loggedIn at the failure, then at each look
        self._login_refusals = 0      # _login_required calls: a retry refused again moves it
        self._login_reason = auth.LOGIN
        self._login_file_seen = False # the file was written (a manual retry is its deletion)
        self._retry_failures = 0      # login retries that failed for another reason, in a row
        self._last_connect_error = None
        # The login-required file clears on the next GOOD result. A file left by an
        # earlier runner (a restart after the fix) is armed too, or the
        # console would say "login required" until the next failure.
        self._restore_pending = auth.read_login_required(self.home) is not None
        self._auth_turn = None        # the signal seen inside the running turn
        self._fresh_pending = None    # a start the login held: _start_fresh's with_digest
        self._opened = False          # a client connected at least once
        self._bg = set()              # tasks _record starts (the early interrupt)
        self._fallback_said = False   # the registry-fallback notice, once per runner
        # The home's .mcp.json (mcp_config.py), read at the first options()
        # and kept for the runner's life: a reconnect or a rollover offers
        # the same set, and an edit lands at the next start.
        self._user_mcp = None
        # Restart with resume (data/runner-session.json): the id on file
        # (cached), a write the loop owes the file, and the id the first
        # init after a resume must name.
        self._saved = None
        self._saved_lane = "unknown"  # the lane on file (cached: options() reads no file)
        self._pending_save = None
        self._expect_session = None
        self._resume_lost = False     # that init named another id: start fresh
        # (row, envelope text) for every row written this runner turn, added
        # BEFORE its query: the prompt hook can fire before the echo.
        self._sent = []
        self.policy = policy if policy is not None else Policy.load(self.home)
        self.registry = registry
        slug, name = self._identity()
        from cousin_lib.runner.main import root_for
        self.root = root_for(self.home)
        # [agent] commit_attribution (runner_for checked it,
        # like effort): resolved once here, not per options() call, so a
        # cousin.toml edited mid-session never changes it mid-turn. A
        # caller that builds a runner directly (most tests) leaves it
        # unset and gets it resolved from cousin.toml / config/harness.toml.
        self.commit_attribution = (self._commit_attribution() if commit_attribution is None
                                   else bool(commit_attribution))
        self.tool_context = tools.ToolContext(home=self.home, slug=slug, name=name,
                                              root=self.root, turn=self.turn,
                                              policy=self.policy, stream=self.stream,
                                              registry=registry, session=session)
        # One per runner, not per connect: a rollover or a reconnect builds
        # new hooks, and they must keep the baseline and the tightened
        # policy (config_watch), never fall back to the start's.
        self.config_watch = config_watch.ConfigWatch(self.home, self.root)
        self.memory_watch = memory_watch.MemoryWatch(self.home)
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
        # The review gate (review_gate.py): after every turn, and once at
        # start, entries over [memory] review_batch are held; a second model
        # (_model_review, or the caller's reviewer: entries -> {id: verdict},
        # sync or async) reviews them in ONE background task on this loop,
        # batch by batch, never holding the next turn (_gate_hold).
        self.memory_reviewer = memory_reviewer or self._model_review
        self._review_queue = []
        self._review_task = None

    def _identity(self):
        """(slug, name) from cousin.toml; the directory name when it lacks them."""
        from cousin_lib.config import CousinConfig
        try:
            cfg = CousinConfig.load(self.home)
            return cfg.slug, cfg.name
        except Exception:  # noqa: BLE001 - a thin toml still runs; the dir names it
            return self.home.name, self.home.name.capitalize()

    def _policy_tightened(self, policy):
        """A policy.toml edit tightened the live policy (hooks, config_watch):
        the runner keeps it, so the next connect's hooks start from it, and
        the tools (the outbound filter) read the same object."""
        self.policy = policy
        self.tool_context.policy = policy

    def _agent_table(self):
        """cousin.toml [agent], or {} when unreadable (a thin toml still
        runs)."""
        import tomllib
        try:
            return tomllib.loads((self.home / "cousin.toml").read_text()).get("agent") or {}
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def _agent_value(self, key, default):
        """A value from cousin.toml [agent], else default."""
        return self._agent_table().get(key, default)

    def _commit_attribution(self):
        """config.commit_attribution, resolved for this cousin: its own
        cousin.toml [agent] commit_attribution wins, else the install's
        config/harness.toml [agent] commit_attribution, else True."""
        from cousin_lib.config import commit_attribution as resolve_commit_attribution
        return resolve_commit_attribution(self.root, self._agent_table())

    # -- options -----------------------------------------------------------
    def options(self, *, resume=None):
        sdk = _sdk()
        # The account's variables to SET (cousin-runner scrubbed every auth
        # variable from its own environment). account_for refused everything
        # but a missing secret before the lock, so what can raise here is
        # SecretMissing (a login to do: _connect waits for it), or a
        # secret broken after the start: a connect failure with its message,
        # never the secret.
        env = dict(accounts.account_env(self.account, self.root), **AUTO_MEMORY_OFF)
        # The tools and hooks are in-process: no settings file is read
        # (setting_sources=[]) and none is written; the policy is a
        # PreToolUse hook, since bypassPermissions skips can_use_tool.
        server = tools.build_tool_server(self.tool_context, self.registry,
                                         on_fallback=self._registry_fallback)
        hook_table = hooks.build_hooks(self.home, slug=self.tool_context.slug, root=self.root,
                                       machine=self.machine, stream=self.stream,
                                       policy=self.policy, lock=self._lock,
                                       body_for_prompt=self._body_for_prompt,
                                       request_rollover=self._request_rollover,
                                       live_threads=lambda: self.tool_context.turn.snapshot()[1],
                                       thread_for_prompt=self._thread_for_prompt,
                                       reply_gate=bool(self._agent_value("reply_gate", True)),
                                       watch=self.config_watch,
                                       policy_changed=self._policy_tightened,
                                       memory_watch=self.memory_watch)
        # The composed prompt (prompt.py): byte-stable across generations,
        # so a rollover and a restart keep the cache.
        # With snapshot=True a resumed session keeps the prompt it first
        # recorded, so an edit to identity files lands at the next rollover.
        # The text is never on the CLI's argv (every local user can read
        # one): system_prompt_option writes it to a private file, and the CLI
        # gets the file's path.
        from cousin_lib.runner import prompt
        system_prompt = prompt.system_prompt_option(self.home, root=self.root,
                                                    registry=self.tool_context.registry)
        # replay-user-messages: the echo is how a turn knows which rows the
        # model actually took in (see the module docstring).
        extra = {"replay-user-messages": None,
                 prompt.APPEND_FILE_FLAG: str(prompt.system_prompt_path(self.home))}
        mcp_servers = self._mcp_options(server, extra)
        store_resume = resume
        if resume and self._resume_via_cli():
            # A login account, or a lane never recorded: the CLI's own --resume.
            # The SDK's store-backed resume would run the CLI under a temporary
            # config dir with the OAuth refresh token stripped. options.resume
            # stays unset so nothing is materialized; session_store stays set,
            # so the store keeps mirroring. The store is NOT the owner here: a
            # lost local transcript is resume_failed, then fresh + digest.
            extra["resume"] = resume
            store_resume = None
        return sdk.ClaudeAgentOptions(cwd=str(self.cwd), model=self.model, env=env,
                                      effort=self.effort,
                                      permission_mode="bypassPermissions",
                                      setting_sources=[], resume=store_resume,
                                      settings=_attribution_settings(self.commit_attribution),
                                      system_prompt=system_prompt, session_store=self.session_store,
                                      mcp_servers=mcp_servers,
                                      hooks=hook_table,
                                      extra_args=extra)

    def _mcp_options(self, server, extra):
        """The `mcp_servers` option, and the file flag added to `extra`.
        Only `cousin` stays in the option, which the SDK writes inline on
        the CLI's argv: the SDK serves an in-process server only from
        there, and its config is a name. Every other server goes to a
        private file (prompt.write_private: 0600 in a 0700 dir), rewritten
        at every options() and named by a second --mcp-config, so a literal
        secret in .mcp.json is on no argv. With none, there is no file."""
        from cousin_lib.runner import prompt
        servers = self._mcp_servers(server)
        inline = {"cousin": servers.pop("cousin")}
        if servers:
            path = prompt.write_private(self.mcp_config_path(),
                                        json.dumps({"mcpServers": servers}, indent=1) + "\n")
            extra[MCP_CONFIG_FLAG] = str(path)
        else:
            self.drop_mcp_config()
        return inline

    def mcp_config_path(self):
        """data/run/mcp-config.json for the primary; a side session keeps
        its own, data/run/mcp-config-<kind>.json, so one session's end
        never removes the file another's CLI is about to read. Absolute:
        the CLI reads it whatever its cwd."""
        path = self.home.joinpath(*MCP_CONFIG_FILE)
        if self.session != PRIMARY:
            path = path.with_name("mcp-config-%s.json" % self.session)
        return Path(os.path.abspath(path))

    def drop_mcp_config(self):
        """Remove the servers' file: the loop's end and stop() call it, so
        it lives as long as the CLI that reads it. A runner killed outright
        leaves it (still 0600), and its next start rewrites it."""
        try:
            self.mcp_config_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.stream.append("error", {"error": "mcp config file: %s" % exc})

    def _mcp_servers(self, server):
        """`cousin` first, then the home's .mcp.json servers by name, then
        the servers of the plugins the cousin enables (mcp_config.add_plugins).
        alwaysLoad is the cousin's alone: the CLI would defer its tools
        behind its tool search, so a cousin's first memory, send or reply
        call would need a search first. A user server's tools stay
        deferred: a server with many tools (or one whose list changes
        between starts) then costs no prompt bytes until the model looks
        one up, and the cached prefix does not move with it."""
        if self._user_mcp is None:
            from cousin_lib.runner import mcp_config
            try:
                self._user_mcp = mcp_config.load(self.home, root=self.root)
            except Exception as err:  # noqa: BLE001 - never fatal: the cousin keeps `cousin`
                self._user_mcp = mcp_config.Loaded(True, skipped=[{
                    "name": None, "reason": "%s not read: %s" % (mcp_config.FILE,
                                                                 type(err).__name__)}])
            if self._user_mcp.present or self._user_mcp.plugins:
                self.stream.append("mcp_config", self._user_mcp.event())
        return dict({"cousin": {**server, "alwaysLoad": True}}, **self._user_mcp.servers)

    # -- the session on file (restart with resume) -----------------------
    def _session_path(self):
        """data/runner-session.json for the primary; a side session keeps its
        own, data/runner-session-<kind>.json."""
        if self.session == PRIMARY:
            return self.home / "data" / "runner-session.json"
        return self.home / "data" / ("runner-session-%s.json" % self.session)

    def _read_session_file(self):
        """The file's dict; {} when it is missing, unreadable or not an
        object (null, a list, a number): a fresh start, never a crash loop."""
        try:
            d = json.loads(self._session_path().read_text())
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def saved_session(self):
        return self._read_session_file().get("session_id") or None

    def saved_lane(self):
        return self._read_session_file().get("lane") or "unknown"

    def resume_lane(self):
        """The lane on record: the init's apiKeySource, as seen this process
        or as persisted with the session (cached when the loop read the file
        at start). A record only: it decides nothing (_resume_via_cli)."""
        return self._lane if self._lane != "unknown" else self._saved_lane

    def _resume_via_cli(self):
        """Resume per KIND: a claude-login account
        refreshes its own token, so it resumes through the CLI's --resume;
        a token or key account never refreshes, so it resumes store-backed.
        Every runner has an account (the host's login when none was given),
        so the kind always decides; the lane on file is a record."""
        return accounts.resume_via_cli(self.account)

    def _save_session(self, session_id):
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        lane = self._lane
        tmp.write_text(json.dumps({"session_id": session_id, "lane": lane,
                                   "generation": self._session_generation(),
                                   "updated": time.time()}))
        tmp.replace(path)
        self._saved, self._saved_lane = session_id, lane

    def _session_generation(self):
        """The generation the session file records: the home's current one.
        A side session records the generation it BELONGS to (sessions.py)."""
        return boot.read_generation(self.home)

    async def _flush_session(self):
        """The pending write, off the loop. A failed write is an `error`
        event and stays pending for the next flush, never a raise."""
        sid, self._pending_save = self._pending_save, None
        if sid is None:
            return
        try:
            await asyncio.to_thread(self._save_session, sid)
        except Exception as exc:  # noqa: BLE001 - the file must never fail a turn or a stop
            if self._pending_save is None:
                self._pending_save = sid
            self.stream.append("error", {"error": "runner-session.json: %s: %s"
                                         % (type(exc).__name__, exc)})

    def _note_session(self, session_id):
        """Every init and every result names the session; this runs on the
        loop, so it only records (the write is _flush_session's). The first
        name after a resume proves the resume: a different id means the
        CLI started a new session, and the start is then a fresh one."""
        if not session_id:
            return
        self._resume_id = session_id
        if self._expect_session is not None:
            asked, self._expect_session = self._expect_session, None
            if session_id != asked:
                self._resume_lost = True
                self.stream.append("system", {"subtype": "resume_failed", "session_id": asked,
                                              "got": session_id,
                                              "error": "the CLI started a new session"})
        # a new id, or the same id on another lane than the file says: a
        # stale lane would pick the wrong resume path at the next start
        lane_moved = self._lane != "unknown" and self._lane != self._saved_lane
        if session_id != self._saved or lane_moved:
            self._pending_save = session_id

    def _has_state(self):
        return (self.home / "STATUS.md").exists() or (self.home / "data" / "handoff.md").exists()

    async def _start_fresh(self, *, with_digest):
        """A generation's start: start hooks once, then the digest as the
        first message when there is state to carry. Runs at a turn boundary
        (the machine idle), never inside a turn: the digest is a turn of its
        own. A failed step is an `error` event, never a raise into the loop.
        A new session is a generation start whether or not the generation
        moved (a first boot does not): recorded for the daily flip."""
        try:
            await asyncio.to_thread(boot.mark_generation_start, self.home)
        except Exception as exc:  # noqa: BLE001 - a record; the session runs on
            self.stream.append("error", {"error": "generation start: %s: %s"
                                         % (type(exc).__name__, exc)})
        try:
            await asyncio.to_thread(session.run_phase, self.home, "start")
        except Exception as exc:  # noqa: BLE001 - the session runs on without its hooks
            self.stream.append("error", {"error": "start hooks: %s: %s"
                                         % (type(exc).__name__, exc)})
        self.stream.append("system", {"subtype": "fresh", "digest": with_digest})
        if not with_digest:
            return
        generation = await asyncio.to_thread(boot.read_generation, self.home)
        digest, _ = await self._digest(generation)   # never raises; degraded when it must
        if digest is None:
            return
        digest, handed = self._handover(digest)
        try:
            digest_id = self.inbox.put(Item(thread_id="system", source="boot", body=digest,
                                            sender="runner"))
        except Exception as exc:  # noqa: BLE001 - the session runs on without it
            self.stream.append("error", {"error": "the digest row could not be stored: %s: %s"
                                         % (type(exc).__name__, exc)})
            return
        if handed:
            # The row is durable: it carries the paragraph now. A crash between
            # the put and this rename hands the paragraph twice at the next
            # fresh start (never zero times): safe, and accepted.
            handover.consume(self.home)
        await self._wait_rate_limit()   # a claim by id skips the loop's wait
        if self._stop.is_set() or self._stopping.is_set():
            return      # the row stays queued (durable): the next start runs it
        # the loop's own guard: this runs outside the loop's per-row try, and a
        # raise here would end the worker (and a restart would start fresh again)
        try:
            first = self.inbox.claim_id(digest_id, claimant=self.session_id)
            if first is not None:
                await self._turn(first)
        except Exception as exc:  # noqa: BLE001 - the idle transition can still raise
            self._fail_turn([], exc)

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
        for a row on a thread that is not operator or person chat (recall
        is only for those) and for a prompt no row
        matches. Runs on the loop thread, the only writer of `_sent`."""
        match = self._row_for_prompt(prompt)
        if match is None:
            return ""
        try:
            kind, _ = parse_thread(match["thread_id"])
        except DeliveryError:
            return ""
        return (match.get("body") or "") if kind in SURFACE_KINDS else ""

    def _row_for_prompt(self, prompt):
        """The row written this runner turn whose envelope text is the
        prompt (see _body_for_prompt), or None."""
        prompt = (prompt or "").strip()
        sent = [(row, text.strip()) for row, text in self._sent]
        match = next((row for row, text in reversed(sent) if text == prompt), None)
        if match is None:
            prefixed = [(len(text), row) for row, text in sent if text and prompt.startswith(text)]
            match = max(prefixed, key=lambda pair: pair[0])[1] if prefixed else None
        return match

    def _thread_for_prompt(self, prompt):
        """The thread id of the row the prompt carries, or None (the reply
        gate resets only that thread's answered state)."""
        match = self._row_for_prompt(prompt)
        return match.get("thread_id") if match else None

    def _on_state(self, old, new, detail):
        self.stream.append("state", {"from": old, "to": new, "detail": detail})

    # -- Runner protocol ---------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def begin_stop(self):
        """A stop was asked for (cousin-runner's signal handler, before
        stop() runs): from now on nothing new is claimed, neither a turn
        nor a fold; what is live is finished or settled by stop()."""
        self._stopping.set()

    def stop(self, *, timeout=30.0):
        if self.machine.state == "stopped":
            return
        self.begin_stop()
        # A running turn is interrupted first, so the join below does not
        # wait on a turn nobody will end (FakeRunner does the same). The CLI
        # records that as the user's stop: leave the mark the next resume
        # answers. A requested stop wrote run/held before its signal:
        # the mark then names that stop, never "not the operator".
        if self.interrupt() and self.takes_restart_note:
            try:
                restart_note.mark(self.home, "a stop interrupted the turn in flight",
                                  held=restart_note.held_by(self.home))
            except OSError as exc:
                self.stream.append("error", {"error": "restart mark: %s" % exc})
        self._stop.set()
        wake.poke(self.home)
        if self._thread is not None:
            self._thread.join(timeout)
        self.turn.end()
        self.drop_mcp_config()       # a runner whose loop never ran (options() alone)
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
        never marks a finished turn interrupted or reaches an idle CLI;
        unless the turn still waits on a row carried past that result:
        written before the interrupt, the row starts the CLI turn
        the interrupt then ends."""
        if not self._interrupt_may_go(seq):
            return False
        self._interrupt_requested = True
        if self._writer is None:
            self._interrupt_sent = True
            await self._client.interrupt()
            return True
        # in line behind any fold already handed over (_Writer)
        return await self._writer.submit(_Job(lambda: self._interrupt_write(seq))).future

    async def _interrupt_write(self, seq):
        """The control write of an interrupt, on the writer: checked again
        when its turn in the queue comes, so one that waited behind a fold
        never reaches a CLI whose turn has ended."""
        if not self._interrupt_may_go(seq):
            return False
        self._interrupt_sent = True     # what _close reads: an interrupt that reached the client
        # written while the CLI is between turns (a carried read) it may
        # cut nothing: the echo sends it again. One to the live turn clears it.
        self._interrupt_idle = not self._live
        with self._waiting_at("interrupt"):     # the stall probe sees the writer too
            await self._client.interrupt()
        return True

    def _interrupt_may_go(self, seq):
        """True when turn `seq` is the live one; a stale request is said as
        `interrupt_dropped` and never reaches the client."""
        if seq != self._turn_seq or self.machine.state not in LIVE_STATES \
                or not (self._live or self._carrying):
            self.stream.append("system", {"subtype": "interrupt_dropped",
                                          "turn": seq, "current": self._turn_seq})
            return False
        return True

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

    def activity(self):
        """What this session is doing now, for another session's digest:
        the machine state, the KINDS of the threads its live
        turn answers (never a key, a sender or a body) and when that turn
        started (epoch seconds), None when no turn is live."""
        active, threads = self.turn.snapshot()
        kinds = []
        for thread in threads if active else ():
            kind = thread.partition(":")[0]
            if kind not in kinds:
                kinds.append(kind)
        return {"state": self.machine.state, "thread_kinds": kinds,
                "since": self._turn_started_at if active else None}

    def _claim(self, limit):
        """The rows this session may take: its kinds only. None
        once a stop was asked for: a claim
        the stop raced goes straight back to the queue."""
        if self._stopping.is_set():
            return []
        rows = self.inbox.claim(limit=limit, claimant=self.session_id,
                                kinds=self.claim_kinds, exclude_kinds=self.exclude_kinds)
        if rows and self._stopping.is_set():
            for row in rows:
                self.inbox.requeue(row["id"])
            return []
        return rows

    def _doorbell(self):
        """The wake socket (one per home, so the primary's); a side session
        overrides it with a poller (runner/sessions.py)."""
        return wake.listen(self.home, self._wake_error)

    async def _boundary(self, row):
        """Runs at a turn boundary with the claimed `row` in hand, before its
        turn; False means the row went back and no turn runs. The primary has
        nothing to do here; a side session resets itself here (sessions.py)."""
        return True

    def unsupported(self):
        return list(self.UNSUPPORTED)

    def plugin_items(self):
        """The contract items a plugin meets (none on this lane); optional in
        the Runner protocol, read by contract_table."""
        return list(self.PLUGIN_ITEMS)

    # -- phase-2 CLI conveniences, NOT in the Runner protocol ------------------
    def worker_alive(self):
        """True while the worker thread runs. `cousin-runner` uses it to
        exit 3 when the worker gave up (a fatal connect failure)."""
        return self._thread is not None and self._thread.is_alive()

    # -- a dying login (auth.py) ---------------------------------------------
    def login_required(self):
        """True while the runner waits for its account's login (or billing)
        to be fixed: `errored`, nothing claimed, never `fatal`."""
        return self._login_blocked

    def _login_action(self, reason):
        """What the operator does, said where the operator looks. The
        runner never obtains a credential: it names the command to run."""
        if reason == auth.BILLING:
            return auth.billing_action(self.account, self.home)
        if self.account.kind != "claude-login" and self.account.secret_file is None:
            # a secret handed over in memory: no file to fix, no mark to watch
            return ("replace the %s this runner was started with and restart it, or delete %s"
                    " to retry" % (self.account.kind, self.home / auth.LOGIN_FILE))
        action = accounts.login_action(self.account, via=self.tool_context.slug)
        if self.account.kind != "anthropic-key":
            action = "run " + action
        if self.account.name == accounts.HOST and not accounts.in_container():
            # in the image the hostname is the container's id, and the
            # action already says where to run it (the Docker host)
            action += " on %s" % auth.host_label(self.root)
        return action

    def _login_required(self, detail, *, reason=auth.LOGIN):
        """The account needs the operator: `errored` with detail
        `login_required` (or `billing`), data/login-required.json, an
        `auth` event, and the loop waits in _await_login. Never `fatal`.
        What the wait compares against (the credential mark, the status) is
        read BEFORE the file is written: a fix made the moment the operator
        sees the file must still read as a change."""
        with self._lock:
            if self.machine.state in ("idle", "rolling_over", "rate_limited") + LIVE_STATES:
                self.machine.to("errored", reason)
        self._login_blocked = True
        self._login_refusals += 1
        self._restore_pending = True
        self._login_mark = auth.credential_mark(self.account, self.root)
        # `claude auth status` (no model call) on the loop thread: a failure
        # path, and nothing runs on this runner until the fix anyway
        try:
            self._login_was_in = bool(accounts.status(self.account, self.root).get("loggedIn"))
        except Exception:  # noqa: BLE001 - no reading is "logged out": a later "in" retries
            self._login_was_in = False
        self._login_reason = reason
        self._retry_failures = 0
        data = self._write_login_file(reason, detail)
        self.stream.append("auth", {k: data[k] for k in ("account", "kind", "reason", "detail",
                                                          "action", "since", "host")})

    def _write_login_file(self, reason, detail):
        """data/login-required.json (keeps `since`); the dict it holds. A
        failed write is an `error` event, and no file means no manual retry
        (_await_login counts a deletion only of a file it saw)."""
        fields = dict(host=auth.host_label(self.root), account=self.account.name,
                      kind=self.account.kind, reason=reason, detail=detail,
                      action=self._login_action(reason))
        try:
            data = auth.write_login_required(self.home, **fields)
            self._login_file_seen = True
        except Exception as exc:  # noqa: BLE001 - the event and the wait still happen
            data = dict(fields, detail=str(detail)[:300],
                        since=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._login_file_seen = False
            self.stream.append("error", {"error": "%s: %s: %s" % (
                auth.LOGIN_FILE, type(exc).__name__, exc)})
        return data

    def _login_retry_failed(self):
        """A login retry that failed for a reason other than the login: the
        file says why (its detail), and after RETRY_FAILURES_TO_FRESH in a
        row _main's rule applies: the session on file is dropped and the
        next retry starts fresh, the digest carrying the state."""
        self._retry_failures += 1
        if self._retry_failures >= self.RETRY_FAILURES_TO_FRESH and self._resume_id:
            self.stream.append("system", {"subtype": "resume_failed",
                                          "session_id": self._resume_id,
                                          "error": "%d login retries failed: %s" % (
                                              self._retry_failures, self._last_connect_error)})
            self._resume_id = self._expect_session = None
            self._fresh_pending = True
            self._retry_failures = 0
        self._write_login_file(self._login_reason, "the retry could not connect: %s"
                               % self._last_connect_error)

    def _login_turn_lost(self, rows, cause, signal):
        """A turn with an auth signal whose stream ended or raised before a
        result: its rows go back (as _close's auth branch does), the runner
        waits for the login, and the client is replaced by the retry."""
        self.turn.end()
        try:
            # the result first, then the rows it names go back, even
            # when the append raises
            try:
                self.stream.append("result", {"inbox_ids": [],
                                              "requeued": [r["id"] for r in rows],
                                              "interrupted": self._interrupt_sent,
                                              "is_error": True, "num_turns": 0,
                                              "total_cost_usd": None, "session_id": None,
                                              "usage": None, "repeat_in_transcript": True,
                                              "auth": signal["reason"],
                                              "error": "%s: %s" % (type(cause).__name__, cause)})
            finally:
                for row in rows:
                    self.inbox.requeue(row["id"])
        except Exception as exc:  # noqa: BLE001 - recorded; the wait still starts
            self.stream.append("error", {"error": "requeueing a turn the login failed: %s: %s"
                                         % (type(exc).__name__, exc)})
        self._interrupt_requested = self._interrupt_sent = False
        self._login_required(signal["detail"], reason=signal["reason"])

    def _note_good_result(self):
        """The first SUCCESSFUL result after a login failure proves the
        login (an init cannot: a live login and a revoked one both read
        "none"), so the file clears here and nowhere else."""
        if not self._restore_pending:
            return
        self._restore_pending = False
        self._login_attempt = 0
        auth.clear_login_required(self.home)
        self.stream.append("auth", {"account": self.account.name, "restored": True})

    def _interrupt_soon(self):
        """From _record, on the loop thread: interrupt the live turn now. A
        401 on the first attempt is final; the CLI's retries would take
        about 3 minutes to say what the first one said."""
        task = asyncio.get_running_loop().create_task(self._interrupt_turn(self._turn_seq))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        task.add_done_callback(self._interrupt_done)

    async def _await_login(self):
        """Wait for the operator's fix without spending a turn. Every
        1, 2, 4 ... 300 s LOOK at the account: its credential mark and
        `claude auth status` (no model call). Reconnect only when the mark
        CHANGED since the failure, the status went from logged out to
        logged in, or the operator deleted data/login-required.json (the
        manual retry; the way out of a billing stop). A revoked login still
        reads logged in (status proves presence): only a new credential
        moves it. Back to idle on a connect; the next good result clears
        the file. The mark and the status it compares against were
        read at the failure (_login_required), each look moves the status.
        Between two looks the cheap signals (the mark, the file) are read
        every login_poll_s: a change since the last look ends the wait at
        once, so a login finished mid-backoff is seen within a second."""
        again, seen = None, self._login_file_seen
        base = self._login_mark             # what the quick reads compare against
        while not self._stop.is_set() and self._login_blocked:
            await self._login_wait(auth.backoff_s(self._login_attempt), base, seen)
            if self._stop.is_set():
                return
            self._login_attempt += 1
            mark = await asyncio.to_thread(auth.credential_mark, self.account, self.root)
            st = await asyncio.to_thread(accounts.status, self.account, self.root)
            now_in = bool(st.get("loggedIn"))
            present = auth.read_login_required(self.home) is not None
            why = ("credentials changed" if mark != self._login_mark
                   else "logged in" if (now_in and not self._login_was_in)
                   else "manual retry" if (seen and not present)
                   else again)
            self._login_was_in, seen, base = now_in, present, mark
            if why is None:
                continue                    # nothing the operator did yet: no connect, no turn
            refusals = self._login_refusals
            # the backoff keeps growing until a GOOD result (_note_good_result):
            # a login that connects and fails every turn must not retry every second
            if await self._login_retry():
                self._login_blocked = False
                with self._lock:
                    if self.machine.state == "errored":
                        self.machine.to("idle", "login retry: %s" % why)
                self.stream.append("auth", {"account": self.account.name, "retry": why})
                fresh, self._fresh_pending = self._fresh_pending, None
                if fresh is not None:       # the start the login held back
                    await self._start_fresh(with_digest=fresh)
                return
            # refused for the login again: _login_required took a fresh mark and
            # only the next change moves the runner; any other failure (a secret
            # file half written, a CLI that did not start, a session gone): the
            # next look retries, and the file says why
            if self._login_refusals != refusals:
                again, base = None, self._login_mark
            else:
                again = why
                self._login_retry_failed()
            seen = self._login_file_seen

    async def _login_wait(self, seconds, base, seen):
        """Sleep up to `seconds` (the backoff), reading the cheap signals
        every login_poll_s: the credential mark (a stat and a small hash)
        and whether data/login-required.json is there. Return early when the
        mark moved from `base` (the last look's) or the file `seen` there is
        gone (the manual retry, or `login --via` waking this cousin). The
        look that follows decides; `claude auth status` stays on the
        backoff (it starts the CLI)."""
        deadline = time.monotonic() + seconds
        poll_at = time.monotonic() + self.login_poll_s
        while not self._stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            if time.monotonic() < poll_at:
                continue
            poll_at = time.monotonic() + self.login_poll_s
            if seen and not (self.home / auth.LOGIN_FILE).exists():
                return
            mark = await asyncio.to_thread(auth.credential_mark, self.account, self.root)
            if mark != base:
                return

    async def _login_retry(self):
        """One reconnect after the operator's fix; True when a client
        connected. A start that never connected keeps _main's rule: a saved
        session that does not come back, for a reason other than the login,
        gives way to a fresh one with the digest carrying the state."""
        await self._disconnect()
        self._client = None
        resume, refusals, opened = self._resume_id, self._login_refusals, self._opened
        if await self._connect(resume=resume, why="login retry", fatal=False,
                               event="resume_failed" if resume else "connect_failed"):
            if resume and not opened:       # the start's resume, held by the login until now
                self.stream.append("system", {"subtype": "resumed", "session_id": resume})
            return True
        if self._login_refusals == refusals and resume and not opened:
            self._resume_id = self._expect_session = None
            self._fresh_pending = True
        return False

    def _restart_line(self, resumed):
        """A resumed session whose last turn a restart cut gets one
        runner line first (the restart_note row); a fresh one only drops the
        mark. The primary's alone."""
        if not self.takes_restart_note:
            return
        # read, put, then clear: a crash in between repeats the line, never
        # loses it; any failure is recorded and start-up goes on
        try:
            note = restart_note.read(self.home)
            if note is None:
                return
            # the cut turn's tool calls: a fresh session has none of them in
            # its transcript, so it gets the line too when there are any
            ran = self._cut_turn_calls(note)
            if resumed or ran:
                body = restart_note.body(note) if resumed else restart_note.fresh_body(note)
                if ran:
                    body += "\n\n" + ran
                self.inbox.put(Item(thread_id="system", source=restart_note.SOURCE,
                                    body=body, sender="runner"))
                self.stream.append("system", {"subtype": "restart_note", "at": note.get("at"),
                                              "why": note.get("why"), "held": note.get("held"),
                                              "resumed": bool(resumed),
                                              "tools": len(tool_ledger.calls(self.home))})
            restart_note.clear(self.home)
            tool_ledger.clear(self.home)
        except Exception as exc:  # noqa: BLE001 - a lost line must not stop the start
            self.stream.append("error", {"error": "restart note: %s: %s"
                                         % (type(exc).__name__, exc)})

    def _cut_turn_calls(self, note):
        """The restart line's list of the cut turn's tool calls (tool_ledger),
        or "". A death's sweep requeued the cut turn's rows (note
        `requeued`): its message comes again. A ledger whose turn rows are
        all closed then belongs to an older, finished turn (a death between
        a claim and the ledger's begin): no list. A stop closes the cut
        turn's rows as delivered: the list quotes the message instead."""
        ledger_turn = tool_ledger.turn(self.home)
        if ledger_turn is None:
            return ""
        states = [(self.inbox.get(i) or {}).get("state") for i in ledger_turn.get("ids") or []]
        if note.get("requeued"):
            if states and all(s == "done" for s in states):
                return ""
            return tool_ledger.lines(self.home, comes_again=True)
        return tool_ledger.lines(self.home, comes_again=False)

    # -- the loop ------------------------------------------------------------
    def _run_loop(self):
        # asyncio.Runner's close cancels leftover tasks, finalizes async
        # generators and joins the default executor before closing the loop.
        try:
            with asyncio.Runner() as runner:
                self._loop = runner.get_loop()
                runner.run(self._main())
        finally:
            self.drop_mcp_config()

    async def _main(self):
        watchdog = asyncio.ensure_future(self._stall_watch())
        try:
            # the start-up sweep first, before a resume or a fresh start: what
            # a dead runner wrote and never held is held before this session's
            # digest is built. It is the primary's alone: a side session only
            # holds, and reviews what its own gate held.
            await self._gate_hold(sweep=self.sweeps_at_start)
            on_file = await asyncio.to_thread(self._read_session_file)
            saved = on_file.get("session_id") or None
            self._saved, self._saved_lane = saved, on_file.get("lane") or "unknown"
            resumed = False
            if saved:
                resumed = await self._connect(resume=saved, fatal=False, event="resume_failed")
                if resumed or self._login_blocked:
                    # a resume refused for the login leaves the saved session the
                    # session: the login retry resumes it (_login_retry)
                    self._resume_id = saved
                    self._expect_session = saved    # the first init must name it
                if resumed:
                    self.stream.append("system", {"subtype": "resumed", "session_id": saved})
            await asyncio.to_thread(self._restart_line, resumed)
            if not resumed and not self._login_blocked:
                if not await self._connect() and not self._login_blocked:
                    return
                has_state = await asyncio.to_thread(self._has_state)
                if self._login_blocked:
                    # a login to do before anything runs: the fresh start waits for it
                    self._fresh_pending = bool(saved) or has_state
                else:
                    await self._start_fresh(with_digest=bool(saved) or has_state)
            with self._doorbell() as listener:
                while not self._stop.is_set() and self.fatal is None:
                    if self._login_blocked:
                        await self._pump_stop()       # the login may replace the client
                        await self._await_login()     # no claim, no turn while it holds
                        continue
                    await self._backoff()
                    await self._wait_rate_limit()
                    if self._stop.is_set():
                        break
                    try:
                        rows = self._claim(1)
                    except Exception as exc:  # noqa: BLE001 - a store failure is never silence
                        self._fail_turn([], exc)
                        await asyncio.sleep(0.2)  # a wedged store must not spin the loop
                        continue
                    if not rows:
                        self._pump_start()            # the CLI is not always quiet
                        await asyncio.get_running_loop().run_in_executor(
                            None, listener.wait, self.poll_s)
                        continue
                    await self._pump_stop()           # the row's turn reads from here on
                    if rows[0]["source"] == INTERRUPT:
                        # between turns: nothing to interrupt, and never a turn
                        self.inbox.done(rows[0]["id"], FAILED, NO_TURN)
                        continue
                    try:
                        if rows[0]["source"] != "flip":
                            # the daily cost cap, read now (cost_cap): a refused
                            # row is closed and no turn runs
                            row = await asyncio.to_thread(cost_cap.admit, self.home, rows[0],
                                                          inbox=self.inbox, stream=self.stream,
                                                          root=self.root)
                            if row is None:
                                continue
                            rows[0] = row
                        if rows[0]["source"] == "flip":
                            ok = await self._rollover_row(rows[0])
                        elif await self._boundary(rows[0]):
                            ok = await self._turn(rows[0])
                        else:
                            ok = False
                    except Exception as exc:  # noqa: BLE001 - the idle transition can still raise
                        # `[]`: its rows are already closed (FakeRunner._fail_turn)
                        self._fail_turn([], exc)
                        ok = False
                    await self._gate_hold()          # after every turn, a result or an error
                    self._failures = 0 if ok else self._failures + 1
                    if self._resume_lost:
                        # The resume came back as a new session. Known only
                        # from the first init, inside that turn, so the fresh
                        # start runs here, at the boundary after it: that turn's
                        # row ran on the new session and is not lost. A login the
                        # same turn failed holds it (the retry starts it, as any
                        # start the login held); a worker that gave up starts none.
                        self._resume_lost = False
                        if self._login_blocked:
                            self._fresh_pending = True
                        elif self.fatal is None:
                            await self._start_fresh(with_digest=True)
                            # the digest is queued (or run): the new id may go on file
                            await self._flush_session()
        finally:
            watchdog.cancel()
            await self._pump_stop()
            await self._stop_review()
            if not self._resume_lost:
                # a stop never loses the last id; a lost resume's new id waits
                # for the fresh start, which a stop before it leaves to the
                # next start (the old id on file takes the lost-resume path)
                await self._flush_session()
            await self._disconnect()

    @contextlib.contextmanager
    def _waiting_at(self, site):
        """A turn waits at `site`: the watchdog names it while it
        runs past STALL_REPORT_S, and its end is named with its duration."""
        mark = {"site": site, "since": time.monotonic(), "said": False}
        self._waits.append(mark)
        try:
            yield
        finally:
            self._waits.remove(mark)
            took = time.monotonic() - mark["since"]
            if took >= STALL_REPORT_S:
                self.stream.append("system", {"subtype": "stall", "site": site,
                                              "seconds": round(took, 1), "ongoing": False})

    async def _stall_watch(self):
        while True:
            await asyncio.sleep(STALL_CHECK_S)
            now = time.monotonic()
            for mark in list(self._waits):
                if not mark["said"] and now - mark["since"] >= STALL_REPORT_S:
                    mark["said"] = True
                    self.stream.append("system", {"subtype": "stall", "site": mark["site"],
                                                  "seconds": round(now - mark["since"], 1),
                                                  "ongoing": True})

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

    def _on_rate_limit(self, info):
        """Every RateLimitEvent is a `rate_limit` event (a warning too: the
        operator sees it coming). A rejection sets the window; a running
        turn becomes `rate_limited`. A rejection the overage absorbs does
        not block: the request goes through, and the event says so."""
        overage = info.status == "rejected" and info.overage_status == "allowed"
        self.stream.append("rate_limit", {"status": info.status, "resets_at": info.resets_at,
                                          "type": info.rate_limit_type,
                                          "utilization": info.utilization, "overage": overage})
        if info.status != "rejected" or overage:
            return          # a warning, or a rejection the overage absorbs
        self._limited_until = float(info.resets_at or (time.time() + 60))
        with self._lock:
            if self.machine.state == "running":
                self.machine.to("rate_limited", "resets at %s" % datetime.fromtimestamp(
                    self._limited_until, timezone.utc).strftime("%H:%M:%S UTC"))

    async def _wait_rate_limit(self):
        """Claim nothing until the window reopens. Every claim waits here:
        the loop's, and the two claims by id (a fresh start's digest, the
        rollover's). A stop ends the wait and leaves the state to stop()."""
        if self._limited_until is None:
            return
        while not self._stop.is_set() and time.time() < self._limited_until:
            await asyncio.sleep(min(1.0, max(0.05, self._limited_until - time.time())))
        if self._stop.is_set():
            return
        self._limited_until = None
        with self._lock:
            if self.machine.state == "rate_limited":
                self.machine.to("idle", "window reopened")

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
            self._opened = True
            return True
        except Exception as exc:  # noqa: BLE001 - a runner that cannot connect says so
            message = "%s: %s" % (type(exc).__name__, exc)
            self._last_connect_error = message
            if isinstance(exc, accounts.SecretMissing) or auth.is_auth_text(message):
                # the fallback signal (a logged-out connect usually SUCCEEDS and
                # says it in the turn): a login to do, never self.fatal
                self._client = None
                self._login_required(message)
                return False
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

    def _prepare(self, row):
        """The row's message and the exact text its echo will carry,
        noted in `_sent` before any write: the prompt hook may fire first.
        `_Unrenderable` when the row cannot be rendered."""
        try:
            message = envelope.render_message(self._row_item(row))
        except Exception as exc:  # noqa: BLE001 - any rendering failure is the row's
            raise _Unrenderable(row, exc) from exc
        # The SDK's str path sets this key and its iterable path does not.
        message.setdefault("parent_tool_use_id", None)
        text = message["message"]["content"][0]["text"]
        self._sent.append((row, text))
        return message, text

    async def _send(self, sdk, row, open_rows):
        """Write one row into the client and wait for the write: the turn's
        first row and the handoff request, written before anything is read.
        On success the row joins `open_rows` with the exact text its echo
        will carry. A query() that raised before anything was written
        raises `_NotWritten`."""
        message, text = self._prepare(row)
        try:
            await self._write(sdk, row, message)
        except _NotWritten:
            raise
        except Exception:
            open_rows.append((row, text))   # it may have reached the CLI
            raise
        open_rows.append((row, text))

    def _send_later(self, sdk, row, open_rows):
        """Hand one folded row to the turn's writer and return at once: the
        reader keeps reading while it is written (_Writer). The row joins
        `open_rows` now, so the turn cannot end with it in flight; it is
        closed, as every row is, by the first result after its echo. A
        write that raises before anything was written takes it back out,
        lists it in `_unwritten` (every such row is requeued once by the
        turn's failure path) and fails the turn; any other failure leaves
        it open (it may have reached the CLI) and fails the turn with it; a
        write never begun when the writer closes goes back to the queue.
        Raises, with the row in no list, when it could not be handed over."""
        message, text = self._prepare(row)
        entry = (row, text)

        def forget():
            if entry in open_rows:
                open_rows.remove(entry)
                return True
            return False

        def on_error(exc):
            if entry not in open_rows:
                return      # already closed or requeued (a login's result): nothing to fail
            if isinstance(exc, _NotWritten):
                forget()
                self._unwritten.append(row)
            if self._write_error is None:
                self._write_error = exc

        def on_dropped(started):
            # cut off mid-write it may have reached the CLI: it stays open,
            # and the turn's failure path fails it; never begun, it goes back
            if not started and forget():
                self.inbox.requeue(row["id"])
        self._writer.submit(_Job(lambda: self._write(sdk, row, message),
                                 on_error=on_error, on_dropped=on_dropped))
        # after the submit: no yield in between, so the writer cannot have
        # run it yet, and a refused submit leaves the row in no list
        open_rows.append(entry)

    async def _write(self, sdk, row, message):
        """The query() of one message; `_NotWritten` when it raised before
        the transport wrote a byte."""
        yielded = []

        async def one():
            # `query` takes `str | AsyncIterable[dict]`; a bare dict would
            # reach its `async for` and raise TypeError.
            yielded.append(True)
            yield message

        gen = one()
        try:
            with self._waiting_at("send"):
                await self._client.query(gen)
        except Exception as exc:
            if not yielded or _nothing_written(sdk, exc):
                raise _NotWritten(row, exc) from exc
            raise
        finally:
            await gen.aclose()

    async def _fold(self, sdk, open_rows):
        """Operator, person and peer chat that arrived during the live
        turn is handed to the turn's writer, each through
        `_send_later`, never awaited here; a row that cannot be rendered is
        failed and the rest of the claim folds on; anything else goes back
        to the queue (`base.FOLDED_KINDS` says why)."""
        rows = self._claim(10)
        for i, row in enumerate(rows):
            if row["source"] == INTERRUPT or self._interrupt_requested \
                    or not folds_into_turn(row["source"], row["thread_id"]):
                # an interrupt row is _take_interrupts' (it runs every poll)
                self.inbox.requeue(row["id"])
                continue
            # handed to the writer, never awaited here: this runs on the
            # reader's path (_next), and a write blocked on a full stdout
            # would stop the reader that drains it (_Writer)
            try:
                self._send_later(sdk, row, open_rows)
            except _Unrenderable as exc:
                # not transient: this row fails, the live turn goes on, and
                # the rest of this claim folds at once
                try:
                    self.inbox.done(row["id"], FAILED, str(exc))
                    self.stream.append("error", {"error": "fold: %s" % exc,
                                                 "inbox_id": row["id"]})
                except Exception:
                    for rest in rows[i + 1:]:   # never stranded behind a failed close
                        self.inbox.requeue(rest["id"])
                    raise
                continue
            except Exception:
                # this row (it could not be handed over) and the rest,
                # claimed here and never offered: back to the queue
                for rest in rows[i:]:
                    self.inbox.requeue(rest["id"])
                raise

    async def _take_interrupts(self):
        """Interrupt rows, taken on every poll of a live turn
        whatever the fold's gates: after the first result a folded
        follow-up can start a CLI turn of its own, and only this path can
        stop it. Taken only while the CLI is generating (`_live`) or the
        turn waits on a carried row: one that lands between a result
        and the next turn waits queued, and one no live turn takes is
        closed NO_TURN at the turn boundary. A refused
        interrupt (the CLI raised) fails its own row and never the turn,
        as the in-process path records it and goes on; the turn is then
        not marked interrupted."""
        if not self.takes_interrupts or not (self._live or self._carrying) \
                or self.machine.state not in LIVE_STATES:
            return
        # read off the loop: at poll_s for the whole turn, and a busy
        # inbox waits up to sqlite's lock timeout, which must not freeze the
        # reader, the hooks and the stall watch with it
        rows = await asyncio.to_thread(self.inbox.open_rows, INTERRUPT)
        if not rows or not (self._live or self._carrying) \
                or self.machine.state not in LIVE_STATES:
            return
        for row in rows:
            if row["state"] != "queued" or \
                    self.inbox.claim_id(row["id"], claimant=self.session_id) is None:
                continue
            if self._interrupt_requested:
                self.inbox.done(row["id"], DELIVERED, "the live turn was already being interrupted")
                continue
            if self._writer is None or not self._interrupt_may_go(self._turn_seq):
                self.inbox.requeue(row["id"])
                continue
            # The flag at once, as before: nothing more is folded. The
            # control write goes to the writer, behind any fold already
            # handed over; this runs on the reader's path and never waits.
            self._interrupt_requested = True
            seq = self._turn_seq
            try:
                self._writer.submit(_Job(lambda: self._interrupt_write(seq),
                                         on_ok=self._interrupt_row_ok(row),
                                         on_error=self._interrupt_row_refused(row),
                                         on_dropped=self._interrupt_row_dropped(row)))
            except RunnerError:
                # the writer has ended: the row goes back (the boundary
                # closes it NO_TURN), nothing was asked, and the turn fails
                self._interrupt_requested = False
                self.inbox.requeue(row["id"])
                raise

    def _interrupt_row_ok(self, row):
        def ok(went):
            if went:
                self.inbox.done(row["id"], DELIVERED, "interrupted the live turn")
            else:           # the turn ended first: the boundary closes it NO_TURN
                self.inbox.requeue(row["id"])
        return ok

    def _interrupt_row_dropped(self, row):
        def dropped(started):
            # Cut off mid-write by the turn's end: DELIVERED, not requeued.
            # The control request was handed to the client while its turn
            # was live, and that turn is over, which is what it asked for;
            # a requeue would close it FAILED "no turn was running", untrue
            # when it was taken, and an interrupt never reruns as a turn.
            # Never begun: back to the queue, closed NO_TURN at the boundary.
            if started:
                self.inbox.done(row["id"], DELIVERED, "written as the turn ended")
            else:
                self.inbox.requeue(row["id"])
        return dropped

    def _interrupt_row_refused(self, row):
        def refused(exc):
            # a refused interrupt fails its own row, never the turn
            self._interrupt_requested = False
            self._interrupt_sent = False
            message = "interrupt: %s: %s" % (type(exc).__name__, exc)
            self.stream.append("error", {"error": message})
            self.inbox.done(row["id"], FAILED, message)
        return refused

    async def _next(self, it, started, fold, control=None):
        """The next message, or `_END` when the stream stops. The idle
        timeout bounds the wait for THIS message (a stream gone silent),
        tool_idle_timeout_s while a tool call is open;
        the optional turn timeout bounds the whole turn. `control` (the
        interrupt rows) runs once immediately on every call, i.e. on every
        message received (its throttle, `last_control`, is local to this
        call, so it always fires right away), and then again every
        `poll_s` while this call waits for the next one. `fold` (None once
        folding is over) is throttled by `self._last_fold`, turn-instance
        state reset once at the start of the turn, not per call: it fires
        immediately on the first message after that reset, but from then
        on only every `poll_s` of real time, whether that spans one
        message or several. Either way, a row that lands during a long
        generation is acted on when it lands, not when the next message
        happens to arrive."""
        task = asyncio.ensure_future(it.__anext__())
        # a tool call open is silent while it runs: its own bound
        idle_s = self.tool_idle_timeout_s if self._open_tools else self.idle_timeout_s
        idle_deadline = time.monotonic() + idle_s
        last_control = 0.0
        try:
            while True:
                self._raise_write_error()
                end = self._carried_wait() if self._carrying else None
                if end is not None:
                    if not task.done():
                        return end
                    # a read already completed is handed back first: it may
                    # be the echo that ends the carried read
                    try:
                        return task.result()
                    except StopAsyncIteration:
                        return _END
                if control is not None and time.monotonic() - last_control >= self.poll_s:
                    last_control = time.monotonic()
                    with self._waiting_at("control"):
                        await control()
                if fold is not None and time.monotonic() - self._last_fold >= self.poll_s:
                    self._last_fold = time.monotonic()
                    with self._waiting_at("fold"):
                        await fold()
                now = time.monotonic()
                wait, overrun = idle_deadline - now, "no message for %.1fs%s" % (
                    idle_s, " with a tool call open" if self._open_tools else "")
                if self.turn_timeout_s is not None:
                    left = started + self.turn_timeout_s - now
                    if left < wait:
                        wait, overrun = left, "turn exceeded %.1fs" % self.turn_timeout_s
                if wait <= 0:
                    raise RunnerError(overrun)
                if fold is not None or control is not None:
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

    def _carried_wait(self):
        """The bounds of a carried read; None while it waits on. A
        stop gives the echo min(drain_timeout_s, CARRY_STOP_GRACE_S) (its
        own interrupt may be what makes the CLI take the row), then
        `_CARRY_STOPPED`. An interrupt taken meanwhile gives it
        drain_timeout_s, counted from the request (one stuck behind a
        blocked fold write is bounded too), then `_CARRY_DROPPED`."""
        now = time.monotonic()
        if self._stop.is_set():
            if self._carry_stop_at is None:
                self._carry_stop_at = now + min(self.drain_timeout_s, CARRY_STOP_GRACE_S)
            return _CARRY_STOPPED if now >= self._carry_stop_at else None
        if self._interrupt_requested and self._carry_until is None:
            self._carry_until = now + self.drain_timeout_s
        if self._carry_until is not None and now >= self._carry_until:
            return _CARRY_DROPPED
        return None

    async def _end_carry(self, end, rows):
        """The turn stopped waiting on carried `rows` the CLI never took
        up (no echo): back to the queue, as the tmux kind's stop requeues an
        untaken row. Not a failure: a stop or the user's interrupt, so no
        `errored`, no failure count, and the result cut nothing. After an
        interrupt the client that may still hold them is replaced first
        (a reconnect resuming the session), so a late echo can never run
        them a second time; a stop's teardown disconnects it anyway."""
        why = ("stopped before the CLI took them up" if end is _CARRY_STOPPED else
               "interrupted, and the CLI never took them up (no echo for %.1fs)"
               % self.drain_timeout_s)
        try:
            if end is _CARRY_DROPPED and not self._stop.is_set():
                resume = self._resume_id
                await self._disconnect()
                if await self._connect(resume=resume, why="carried rows never taken up"):
                    self.stream.append("system", {"subtype": "reconnected", "why": why,
                                                  "resumed": resume})
        finally:
            try:
                # the result first, then the rows go back
                self.stream.append("result", {"inbox_ids": [],
                                              "requeued": [r["id"] for r in rows],
                                              "interrupted": False, "is_error": False,
                                              "num_turns": 0, "total_cost_usd": None,
                                              "session_id": None, "usage": None,
                                              "carried": why})
            finally:
                for row in rows:
                    self.inbox.requeue(row["id"])

    def _reinterrupt(self):
        """The carried row's echo came after an interrupt written while
        the CLI was between turns, which may have cut nothing: the same
        interrupt again, to the turn that is now live, on the writer."""
        if self._reinterrupting or self._writer is None:
            return
        self._reinterrupting = True
        seq = self._turn_seq
        try:
            self._writer.submit(_Job(lambda: self._interrupt_write(seq)))
        except RunnerError as exc:      # the writer has ended: the turn fails on it anyway
            self.stream.append("error", {"error": "interrupt again: %s" % exc})

    def _raise_write_error(self):
        """A fold write that failed on the writer fails the turn here, on
        the reader, as it did when the reader wrote it itself. So does a
        writer whose task ended on its own: nothing handed to it would be
        written, and the turn fails now rather than at the idle timeout."""
        exc, self._write_error = self._write_error, None
        if exc is not None:
            raise exc
        why = self._writer.ended() if self._writer is not None else None
        if why is not None:
            raise RunnerError(why)

    async def _close_writer(self):
        writer, self._writer = self._writer, None
        if writer is not None:
            await writer.close()

    def _note_tools(self, sdk, msg):
        """The turn's open tool calls: a tool_use opens one, its
        tool_result closes it, a result closes them all (the CLI's turn is
        over)."""
        if isinstance(msg, sdk.ResultMessage):
            # the ledger stays: an interrupt ends in a result too, and the
            # cut turn's calls are what the next start must see. The next
            # turn's begin empties it.
            self._open_tools.clear()
            return
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, sdk.ToolUseBlock):
                self._open_tools.add(block.id)
                self._ledger(tool_ledger.started, block.id, block.name,
                             getattr(block, "input", None))
            elif isinstance(block, sdk.ToolResultBlock):
                self._open_tools.discard(block.tool_use_id)
                self._ledger(tool_ledger.finished, block.tool_use_id,
                             error=bool(getattr(block, "is_error", False)))

    def _ledger(self, fn, *args, **kw):
        """The primary's turn ledger (tool_ledger): what a restart must not
        repeat. A side session has no restart note to put it in. A write
        that fails is recorded and the turn goes on."""
        if not self.takes_restart_note:
            return
        try:
            fn(self.home, *args, **kw)
        except OSError as exc:
            self.stream.append("error", {"error": "tool ledger: %s" % exc})

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
        message = str(exc) if isinstance(exc, _Unrenderable) \
            else "%s: %s" % (type(exc).__name__, exc)
        # The state first: whoever sees the `error` event also sees `errored`.
        with self._lock:
            if self.machine.state in ("idle",) + LIVE_STATES:
                self.machine.to("errored", message)
        self.turn.end()
        self.stream.append("error", {"error": message})
        try:
            # the result first, then the rows it names, closed even
            # when the append raises
            try:
                self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                              "requeued": [r["id"] for r in requeued],
                                              "interrupted": self._interrupt_sent,
                                              "is_error": True, "num_turns": 0,
                                              "total_cost_usd": None, "session_id": None,
                                              "usage": None})
            finally:
                for row in consumed:
                    self.inbox.done(row["id"], FAILED, message)
                for row in requeued:
                    self.inbox.requeue(row["id"])
        except Exception as close_exc:  # noqa: BLE001 - recorded; the resync still runs
            self.stream.append("error", {"error": "closing a failed turn: %s: %s"
                                         % (type(close_exc).__name__, close_exc)})
        if recover:
            self._recover()

    def _recover(self):
        if self._stop.is_set() or self.fatal is not None or self._login_blocked:
            return      # a held login leaves `errored` only through _await_login
        with self._lock:
            if self.machine.state == "errored":
                self.machine.to("idle", "recovered")

    def _pump_start(self):
        """Read the CLI's stream between turns. The CLI is not quiet
        there: a background task (a subagent, a background shell) streams
        its progress, and each completion starts a CLI turn of its own (a
        task notification). Unread, the SDK's message buffer (100) fills,
        its reader blocks, and the control requests behind it go unread:
        every hook and in-process tool call of the background task times
        out. A pump that ended (the stream stopped) is not restarted until
        a turn has used the client."""
        if self._pump is None and self._client is not None and self.fatal is None:
            self._pump = asyncio.ensure_future(self._pump_run())

    async def _pump_stop(self):
        """Stop the pump before anything else reads or replaces the client.
        A CLI turn of its own still open is left to the next reader: the
        row written next is folded into it or queued after it, and `_turn`
        closes the row on the result its echo precedes. A read cut at
        the instant a message arrived can lose that message, as any
        cancelled read of the SDK's stream can (`_next`)."""
        task, self._pump = self._pump, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 - the pump records its own failures
            pass
        if self._pump_turn_open:
            self._pump_turn_open = False
            self.stream.append("system", {"subtype": "background_turn", "phase": "handed_over"})

    async def _pump_run(self):
        """Record every message read between turns, as a turn records its
        own; a CLI turn of its own is a `background_turn` start and a
        `result` with no inbox ids and `background: True`, followed by the
        usual post-result work (`_after_turn`). Ends when the stream stops
        without a result (the CLI died: the next turn finds out and
        recovers) or on a read failure, said as an `error` event."""
        sdk = _sdk()
        try:
            while True:
                got_result = False
                responses = self._client.receive_response()
                try:
                    async for msg in responses:
                        # the model's own message opens a CLI turn of its own;
                        # a subagent's (parent_tool_use_id set) or a system
                        # message (task progress) opens none
                        own = isinstance(msg, (sdk.AssistantMessage, sdk.UserMessage)) \
                            and getattr(msg, "parent_tool_use_id", None) is None
                        if own and not self._pump_turn_open:
                            self._pump_turn_open = True
                            self.stream.append("system", {"subtype": "background_turn",
                                                          "phase": "start"})
                        self._record(sdk, msg)
                        if isinstance(msg, sdk.ResultMessage):
                            got_result = True
                            self._pump_turn_open = False
                            self.stream.append("result", {
                                "inbox_ids": [], "background": True,
                                "is_error": bool(msg.is_error), "num_turns": msg.num_turns,
                                "total_cost_usd": msg.total_cost_usd,
                                "session_id": msg.session_id, "usage": msg.usage})
                            await self._after_turn(msg)
                finally:
                    await _aclose(responses)
                if not got_result:
                    self.stream.append("system", {"subtype": "background_end"})
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a pump failure is said, never raised
            self.stream.append("error", {"error": "between turns: %s: %s"
                                         % (type(exc).__name__, exc)})

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
                    await self._record_usage(msg)   # a failed turn's cost is still a cost
                    return count
        finally:
            await _aclose(responses)

    async def _turn(self, first):
        """One runner turn: write `first`, fold operator/person/peer chat while
        the turn is live, and read until every written row is closed.
        Returns False when the turn failed or a result was an error."""
        self._interrupt_requested = self._interrupt_sent = False
        self._interrupt_idle = self._reinterrupting = False
        self._auth_turn = None
        self._sent = []
        self._open_tools = set()
        carried = []      # rows the turn stopped waiting on: _end_carry's
        carry_end = None
        open_rows = []    # (row, envelope text): written, not yet closed
        closing = []      # rows a result is closing right now
        ok = True
        sending = False   # `first` reached _send: its own except says where it went
        try:
            sdk = _sdk()
            with self._lock:
                self._turn_seq += 1
                self._live = True
                self.machine.to("running", "turn")
            self.turn.begin(first)
            self._ledger(tool_ledger.begin, [first])
            self._turn_started_at = time.time()
            self.stream.append("turn_start", {"inbox_ids": [first["id"]],
                                              "bodies": [first["body"]],
                                              "thread_id": first["thread_id"]})
            sending = True
            # written and awaited before anything is read: the pump read
            # the stream up to here, and the SDK buffers what comes
            # while the write is awaited
            await self._send(sdk, first, open_rows)
            self._write_error = None
            self._unwritten = []
            self._drop_writes = False
            self._writer = _Writer(lambda text: self.stream.append("error", {"error": text}))
            started = time.monotonic()
            self._last_fold = 0.0
            results = 0

            async def fold():
                # a limited turn folds nothing: no claim while the limit holds;
                # nor does a turn the login failed (its rows go back)
                if self._live and not self._interrupt_requested and results == 0 \
                        and self._limited_until is None and self._auth_turn is None:
                    await self._fold(sdk, open_rows)

            while open_rows:
                echoed = set()
                if results:
                    # rows written, their echo not come when the result did
                    self._carrying = True
                responses = self._client.receive_response()
                it = responses.__aiter__()
                try:
                    while True:
                        msg = await self._next(it, started, fold if results == 0 else None,
                                               control=self._take_interrupts)
                        if msg is _END:
                            # the CLI died: the SDK ends the stream on {"type": "end"}
                            raise RunnerError("stream ended without a result")
                        if msg is _CARRY_STOPPED or msg is _CARRY_DROPPED:
                            carry_end, carried[:] = msg, [row for row, _ in open_rows]
                            open_rows[:] = []
                            break
                        self._note_tools(sdk, msg)
                        echo_of = None
                        if isinstance(msg, sdk.UserMessage):
                            row = self._match_echo(sdk, msg, open_rows, echoed)
                            if row is not None:
                                echo_of = row["id"]
                                echoed.add(echo_of)
                                self._live = True   # the CLI took up a carried row
                                self._carrying, self._carry_until = False, None
                                if self._interrupt_idle:
                                    self._reinterrupt()     # it may have cut nothing
                                if row["id"] != first["id"]:
                                    self.turn.add(row)
                        # a message means the permission was settled; a hook's
                        # own event is not one
                        if not isinstance(msg, getattr(sdk, "HookEventMessage", ())):
                            with self._lock:
                                if self.machine.state == "waiting_permission":
                                    self.machine.to("running", "message")
                        self._record(sdk, msg, echo_of=echo_of)
                        if self._pending_save is not None and not self._resume_lost \
                                and isinstance(msg, sdk.SystemMessage) and msg.subtype == "init":
                            # The id an init named goes on file now, not at the
                            # result: a runner killed inside a new session's first turn
                            # resumes it. At the init only: a write that fails is
                            # retried at the result, never once per message. A lost
                            # resume keeps the old id until its fresh start has run.
                            await self._flush_session()
                        if isinstance(msg, sdk.ResultMessage):
                            results += 1
                            self._live = False
                            self._carrying, self._carry_until = False, None
                            cut = self._interrupt_sent      # _close clears it
                            ok = self._close(msg, open_rows, echoed, closing) and ok
                            self._interrupt_idle = self._reinterrupting = False
                            if cut and open_rows:
                                # an interrupt ended this CLI turn with rows still
                                # carried: the CLI may have dropped them, so the
                                # wait for their echo is bounded
                                self._carry_until = time.monotonic() + self.drain_timeout_s
                            if self._drop_writes:
                                # a login's result requeued every open row: none of
                                # them may still be written during _after_turn
                                self._drop_writes = False
                                await self._close_writer()
                            await self._after_turn(msg)
                            break
                finally:
                    await _aclose(responses)
            await self._close_writer()   # nothing left in it but interrupts: dropped
            if carried:
                rows, carried[:] = list(carried), []
                await self._end_carry(carry_end, rows)
            self._raise_write_error()
        except Exception as exc:  # noqa: BLE001 - a raising turn body is recorded, not lost
            # first: a fold never begun goes back to the queue, one cut off
            # mid-write stays open (it may have reached the CLI)
            await self._close_writer()
            self._write_error = None
            # every fold whose write wrote nothing, once each, plus a first
            # row that was never written
            requeued, self._unwritten = list(self._unwritten), []
            if isinstance(exc, _NotWritten) and all(r["id"] != exc.row["id"] for r in requeued):
                requeued.append(exc.row)
            cause = exc.cause if isinstance(exc, _NotWritten) else exc
            # carried rows the turn stopped waiting on, not yet handed back
            requeued += [row for row in carried if all(r["id"] != row["id"] for r in requeued)]
            # A raise before the send (the move to `running` refused, say) leaves
            # `first` claimed and in no list: back to the queue, the client untouched.
            unsent = [] if sending else [first]
            unclosed = [row for row, _ in open_rows] + closing
            if isinstance(exc, _Unrenderable):
                unclosed.append(exc.row)    # not transient: closed FAILED, never left claimed
            signal, self._auth_turn = self._auth_turn, None
            if signal is not None:
                # the login failed the turn, and then the stream ended (or broke)
                # without a result: still a login, never a failed row
                self._live = False
                self._login_turn_lost(unclosed + requeued, cause, signal)
                return True
            try:
                self._fail_turn(unclosed, cause, recover=False, requeued=requeued + unsent)
            finally:
                self._live = False
                if open_rows or requeued:
                    # the stream may hold this turn's messages, or the client is broken
                    await self._resync()
                self._recover()
            return False
        finally:
            self._carrying, self._carry_until, self._carry_stop_at = False, None, None
            await self._close_writer()   # no writer task outlives its turn
        self.turn.end()
        with self._lock:
            if self.machine.state in LIVE_STATES:
                self.machine.to("idle", "turn done")
        return ok

    def _close(self, msg, open_rows, echoed, closing):
        """Close every row echoed since the last result with this one;
        rows written but not echoed stay open for the next CLI turn."""
        # sent, not asked: one still queued behind a fold when the result
        # came never reached the CLI, and this turn was not interrupted; nor
        # was it when the only one reached the CLI between turns
        interrupted = self._interrupt_sent and not self._interrupt_idle
        is_error = bool(msg.is_error)
        closing[:] = [row for row, _ in open_rows if row["id"] in echoed]
        open_rows[:] = [(row, text) for row, text in open_rows if row["id"] not in echoed]
        # A dying login, before the rate limit: the typed signal
        # seen in the turn decides, not the flag (an interrupted auth turn
        # reads is_error false), else the result's 401 or the CLI's wording.
        signal = self._auth_turn or auth.result_signal(
            is_error, getattr(msg, "api_error_status", None), getattr(msg, "result", None),
            getattr(msg, "errors", None))
        self._auth_turn = None
        if signal:
            # Not the items' fault: back to the queue, rerun after the fix (the
            # row's text repeats). A row written but not echoed goes back too:
            # the client holding it is replaced (_login_retry) before anything runs again.
            rows = closing + [row for row, _ in open_rows]
            ids = [row["id"] for row in rows]
            # the result first, then the rows it names go back and the
            # runner waits for the login, even when the append raises
            try:
                self.stream.append("result", {"inbox_ids": [], "requeued": ids,
                                              "interrupted": interrupted, "is_error": True,
                                              "num_turns": msg.num_turns,
                                              "total_cost_usd": msg.total_cost_usd,
                                              "session_id": msg.session_id, "usage": msg.usage,
                                              "repeat_in_transcript": True,
                                              "auth": signal["reason"]})
            finally:
                for row in rows:
                    self.inbox.requeue(row["id"])
                closing[:], open_rows[:] = [], []
                self._drop_writes = True        # _turn closes the writer before _after_turn
                self._interrupt_requested = self._interrupt_sent = False
                self._login_required(signal["detail"], reason=signal["reason"])
            return True     # the failure counter must not back off on top of the wait
        if not is_error:
            self._note_good_result()        # a good RESULT, never an init
        if is_error and self._limited_until is not None:
            # A rejected request is not the item's fault: back to the queue,
            # claimed again when the window reopens. The result closing it
            # returns True: the failure counter must not back off on top.
            ids = [row["id"] for row in closing]
            # The row's text is in the transcript once already; its rerun
            # writes it a second time. Said here, so the repeat surprises nobody.
            # The result first, then the rows it names go back, even
            # when the append raises.
            try:
                self.stream.append("result", {"inbox_ids": [], "requeued": ids,
                                              "interrupted": interrupted, "is_error": True,
                                              "num_turns": msg.num_turns,
                                              "total_cost_usd": msg.total_cost_usd,
                                              "session_id": msg.session_id, "usage": msg.usage,
                                              "repeat_in_transcript": True})
            finally:
                for row in closing:
                    self.inbox.requeue(row["id"])
                closing[:] = []
                self._interrupt_requested = self._interrupt_sent = False
            return True
        outcome = FAILED if (is_error and not interrupted) else DELIVERED
        ids = [row["id"] for row in closing]
        # the result first: whoever reads a row closed finds the result
        # that closed it. The rows close even when the append raises; a row
        # whose close raises stays in `closing`, and the failure path fails it.
        try:
            self.stream.append("result", {"inbox_ids": ids, "interrupted": interrupted,
                                          "is_error": is_error, "num_turns": msg.num_turns,
                                          "total_cost_usd": msg.total_cost_usd,
                                          "session_id": msg.session_id, "usage": msg.usage})
        finally:
            while closing:
                self.inbox.done(closing[0]["id"], outcome,
                                "turn %s" % (msg.session_id or self.session_id))
                closing.pop(0)
        self._interrupt_requested = self._interrupt_sent = False
        return not (is_error and not interrupted)

    async def _after_turn(self, msg):
        """The single hook for a ResultMessage's post-close work, called
        from `_turn` once per result, right after `_close`: the usage
        record, then extraction of the session's new transcript entries
        (the SDK flushed the store before it yielded the result, so the
        whole turn is there), then the memory proposal (`_propose`). Each
        step runs off the loop (blocking sqlite and file work) and never
        raises into it: a failure is a `usage`, `extract` or `propose`
        event, never a broken turn, and a failed step does not stop the
        next one. (The review gate runs after the whole turn, in `_main`,
        whether it ended in a result or an error: `_gate_hold`.) Last, the context pressure check (rollover.pressure_due,
        held back by the hysteresis): a rollover is requested, never run
        here; the loop claims it at the boundary. First of all, the
        session id this result (or its init) named goes to
        runner-session.json, off the loop; not a lost resume's (the fresh
        start after this turn writes it, once the digest is queued)."""
        if not self._resume_lost:
            await self._flush_session()
        await self._record_usage(msg)
        await self._mine(self._resume_id)
        await self._propose(self._resume_id)
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

    async def _propose(self, sid):
        """Ask the model whether to keep what the turn concluded
        (extract.propose_turn), as a `propose` row the loop runs when
        nothing else waits; the outcome is a `propose` event (`proposal`:
        the row's inbox id, or None), never a raise."""
        store = getattr(self, "session_store", None)
        if not sid or store is None:
            return
        payload = {"session_id": sid, "turn": self._turn_seq}
        try:
            body = await asyncio.to_thread(extract.propose_turn, self.home, sid, store=store,
                                           turn_bodies=self.turn.bodies)
            payload["proposal"] = None
            if body:
                payload["proposal"] = self.enqueue(Item(
                    thread_id="system", source=extract.PROPOSAL_SOURCE, body=body,
                    sender="framework")).inbox_id
        except Exception as exc:  # noqa: BLE001 - a proposal must never fail a turn
            payload.update(proposal=None, error="%s: %s" % (type(exc).__name__, exc))
        self.stream.append("propose", payload)

    # -- the review gate (review_gate.py) ---------------------------------
    async def _gate_hold(self, *, sweep=False):
        """The gate's hold, off the loop: whatever was written on authored
        topics since the per-home cursor, over the batch, is held and queued
        for the reviewer. After every turn (a result or an error: a turn
        that died after its writes is caught here, or at the next start),
        and at start with `sweep`, which also offers the entries left held
        (by a crash, a stop, a failed review) that a reviewer has tried
        fewer than MAX_ATTEMPTS times. Never raises, never awaits a review."""
        try:
            if sweep:
                await asyncio.to_thread(review_gate.begin, self.home)
            held = await asyncio.to_thread(review_gate.hold_new, self.home)
            if sweep:
                held = await asyncio.to_thread(review_gate.to_offer, self.home)
        except Exception as exc:  # noqa: BLE001 - the gate must never fail a turn
            self.stream.append("review_gate", {"turn": self._turn_seq, "held": 0,
                                               "error": "%s: %s" % (type(exc).__name__, exc)})
            return
        if held:
            # one review call per REVIEW_BATCH_MAX rows: a prompt stays bounded
            n = review_gate.REVIEW_BATCH_MAX
            self._review_queue.extend(held[i:i + n] for i in range(0, len(held), n))
            if self._review_task is None or self._review_task.done():
                self._review_task = asyncio.get_running_loop().create_task(self._review_worker())

    async def _review_worker(self):
        """One batch at a time: two reviews over the same held set would
        race in `release`."""
        while self._review_queue:
            await self._review_batch(self._review_queue.pop(0))

    async def _review_batch(self, rows):
        """Review one held batch and settle it; a `review_gate` event when
        it ends (`error` "cancelled" on a stop: the entries stay held)."""
        payload = {"turn": self._turn_seq, "held": len(rows), "kept": 0, "dropped": 0,
                   "pending": len(rows), "error": None}
        try:
            await asyncio.to_thread(review_gate.note_attempt, self.home, rows)
            if asyncio.iscoroutinefunction(self.memory_reviewer):
                verdicts = await self.memory_reviewer(rows)
            else:
                verdicts = await asyncio.to_thread(self.memory_reviewer, rows)
            done, errors = await asyncio.to_thread(
                review_gate.settle, self.home, rows, verdicts or {},
                by="review-gate:%s" % self.session_id, why="the review gate's reviewer",
                model=True)
            values = list(done.values())
            payload.update(kept=values.count("keep"), dropped=values.count("drop"),
                           pending=len(rows) - len(values))
            if errors:
                payload["error"] = "; ".join("%s %s" % kv for kv in errors.items())
        except asyncio.CancelledError:
            payload["error"] = "cancelled"
            self.stream.append("review_gate", payload)
            raise
        except Exception as exc:  # noqa: BLE001 - a failed review leaves entries held
            payload["error"] = "%s: %s" % (type(exc).__name__, exc)
        self.stream.append("review_gate", payload)

    async def _stop_review(self):
        """Cancel the review on a stop: its entries stay held, which is safe."""
        task, self._review_task = self._review_task, None
        self._review_queue = []
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - recorded by the batch
                pass

    REVIEW_TIMEOUT_S = 180.0

    async def _model_review(self, rows):
        """The default reviewer: one tool-less, single-turn call to a fresh
        client on the cousin's account (no MCP servers, no settings, no
        session kept, a fixed cwd under data/), on `[memory] review_model`
        or the cousin's own model, asking for review_gate.parse_verdicts'
        JSON. It runs on this loop, so a stop cancels it and its client
        disconnects; its usage is recorded like a turn's. A raise leaves
        every entry held."""
        return await asyncio.wait_for(self._review_once(rows), self.REVIEW_TIMEOUT_S)

    async def _review_once(self, rows):
        sdk = _sdk()
        env = dict(accounts.account_env(self.account, self.root), **AUTO_MEMORY_OFF)
        cwd = self.home / "data" / "review-cwd"
        await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
        model = await asyncio.to_thread(review_gate.review_model, self.home) or self.model
        options = sdk.ClaudeAgentOptions(cwd=str(cwd), model=model, env=env,
                                         setting_sources=[], tools=[], mcp_servers={},
                                         max_turns=1,
                                         extra_args=dict(BARE_SESSION_ARGS))
        client = self.client_factory(options)
        await client.connect()
        try:
            await client.query(review_gate.review_prompt(rows))
            text = []
            async for msg in client.receive_response():
                if isinstance(msg, sdk.AssistantMessage):
                    text += [b.text for b in msg.content if isinstance(b, sdk.TextBlock)]
                elif isinstance(msg, sdk.ResultMessage):
                    await asyncio.to_thread(
                        usage.record, self.home, client_id="review-gate",
                        session_id=msg.session_id, lane=self._lane,
                        result={"usage": msg.usage, "total_cost_usd": msg.total_cost_usd,
                                "session_id": msg.session_id})
                    if msg.is_error:
                        raise RunnerError("the review turn failed: %s" % (msg.result,))
                    break
        finally:
            await client.disconnect()
        return review_gate.parse_verdicts("\n".join(text), [r["id"] for r in rows])

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
        self._auth_turn = None
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
                if isinstance(msg, sdk.ResultMessage) and self._auth_turn is None:
                    self._auth_turn = auth.result_signal(
                        bool(msg.is_error), getattr(msg, "api_error_status", None),
                        getattr(msg, "result", None), getattr(msg, "errors", None))
                if self._auth_turn is not None:
                    return None     # no handoff from a session that cannot answer
                if isinstance(msg, sdk.ResultMessage):
                    await self._record_usage(msg)      # the handoff turn costs too
                    if not msg.is_error:
                        self._note_good_result()       # a good result proves the login
                    break
        finally:
            await _aclose(responses)
        return self.handoff_box.summary

    async def _ask_handoff(self, reason):
        """'clean' when the handoff tool answered in time; 'emergency' when
        it did not (the file is then written from the store's tail);
        'stopped' when the runner was stopped while waiting; 'rate_limited'
        when a rejected limit held the handoff turn and no summary came;
        'login_required' when the account's login (or billing) failed it."""
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
        if self._limited_until is not None and self.handoff_box.summary is None:
            return "rate_limited"      # a limit is not a refusal: postpone, never an emergency
        if self._auth_turn is not None and self.handoff_box.summary is None:
            return "login_required"    # the login is the problem, not the model: postpone
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
        second rollover. A bequest row is never closed here: it is an
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
        saying where it stopped: never a wedged machine. Once the new
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
            if handoff == "rate_limited":
                # the row waits with everything else; the loop's _wait_rate_limit
                # holds it, then claims it again ahead of chat (priority 0). A resume
                # the handoff turn lost needs no fresh start: the rerun rollover
                # starts the new session itself (as in the login branch below).
                self._resume_lost = False
                self.inbox.requeue(row["id"])
                with self._lock:
                    if self.machine.state == "rolling_over":
                        self.machine.to("idle", "rollover postponed: rate limited")
                self.stream.append("rollover", {"phase": "postponed", "reason": reason,
                                                "until": self._limited_until})
                return True
            if handoff == "login_required":
                # the same shape: the row waits (priority 0, first after the fix),
                # never an emergency handoff; rolling_over -> errored. A resume the
                # handoff turn lost needs no fresh start: the rollover, rerun after
                # the fix, starts the new session itself.
                signal, self._auth_turn = self._auth_turn, None
                self._resume_lost = False
                self.inbox.requeue(row["id"])
                self.stream.append("rollover", {"phase": "postponed", "reason": reason,
                                                "why": signal["reason"]})
                self._login_required(signal["detail"], reason=signal["reason"])
                return True
            # the handoff turn's init may have named another session (a lost
            # resume): that one is the session being ended and restored
            old_sid = self._resume_id or old_sid
            await asyncio.to_thread(session.run_phase, self.home, "end")
            await asyncio.to_thread(rollover.archive_generation, self.home, generation)
            await self._disconnect()
            disconnected = True
            # a final mine: nothing of the old session arrives after this
            await self._mine(old_sid, final=True)
            self._resume_id = None
            self._expect_session, self._resume_lost = None, False   # the new session is fresh
            self._pending_save = None
            await asyncio.to_thread(self._save_session, None)   # cleared before the new client
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
            if self._resume_id != self._saved:
                # the file too: a restart after this resumes the old session
                self._pending_save = self._resume_id
                await self._flush_session()
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
            digest, handed = self._handover(digest)
            try:
                digest_id = self.inbox.put(Item(thread_id="system", source="boot", body=digest,
                                                sender="runner"))
            except Exception as exc:  # noqa: BLE001 - the session runs on without it
                digest_state = "none: the digest row could not be stored: %s: %s" \
                               % (type(exc).__name__, exc)
            if digest_id is not None and handed:
                handover.consume(self.home)
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
        if digest_id is not None:
            await self._wait_rate_limit()   # a claim by id skips the loop's wait
        if self._stop.is_set() or self._stopping.is_set() or digest_id is None:
            return True     # a stored digest row stays queued (durable): the next start runs it
        # The digest is the new session's FIRST message: claimed by id and run
        # now, ahead of chat that queued up during the rollover (same priority,
        # older). The row is durable, so a crash here replays it at the next start.
        first = self.inbox.claim_id(digest_id, claimant=self.session_id)
        if first is not None:
            return await self._turn(first)
        return True

    def _handover(self, digest):
        """(digest, handed): the digest with the previous-transcript
        paragraph appended when a move from the tmux lane left its record
        (handover.py). The caller consumes the record once the row
        holding the paragraph is stored. Never raises."""
        try:
            extra = handover.note(self.home)
        except Exception as exc:  # noqa: BLE001 - the digest goes without it
            self.stream.append("error", {"error": "the previous transcript note: %s: %s"
                                         % (type(exc).__name__, exc)})
            return digest, False
        if extra is None:
            return digest, False
        return digest + extra, True

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
                self._lane = usage.lane_for(d.get("apiKeySource"))
                self.stream.append("session_init", {"apiKeySource": d.get("apiKeySource"),
                                                    "model": d.get("model"),
                                                    "session_id": d.get("session_id"),
                                                    "tools": list(d.get("tools") or []),
                                                    "mcp_servers": list(d.get("mcp_servers") or [])})
                self._note_session(d.get("session_id"))   # after the lane: it is saved with it
                # the account did not take effect: said, nothing more;
                # it can only catch the key kind (a login, a token and a
                # logged-out session all read "none")
                got, want = d.get("apiKeySource"), accounts.expected_source(self.account)
                if got is not None and got != want:
                    self.stream.append("auth", {"account": self.account.name,
                                                "kind": self.account.kind, "expected": want,
                                                "got": got, "mismatch": True})
            elif msg.subtype == "mirror_error":
                # the SDK retried and dropped this batch: the store is missing entries
                self.stream.append("error", {"error": "session store append failed: %s"
                                             % (getattr(msg, "error", "") or msg.data),
                                             "mirror_error": True})
            elif msg.subtype in TASK_SUBTYPES:
                self.stream.append("system", _task_payload(msg.subtype, msg.data))
            else:
                d = msg.data or {}
                payload = {"subtype": msg.subtype}
                if msg.subtype == "api_retry":
                    payload.update(error_status=d.get("error_status"), error=d.get("error"),
                                   attempt=d.get("attempt"))
                    signal = auth.retry_signal(d)
                    # A key or a token cannot refresh, so its first 401 is
                    # final; a claude-login account refreshes at the CLI's next
                    # attempt, so it is left to finish (a refresh that fails ends
                    # in a 401 result, the second signal)
                    if signal and self._auth_turn is None \
                            and not accounts.resume_via_cli(self.account):
                        self._auth_turn = signal
                        if self.machine.state in LIVE_STATES:
                            self._interrupt_soon()      # a bad key fails in seconds
                self.stream.append("system", payload)
        elif isinstance(msg, sdk.AssistantMessage):
            signal = auth.assistant_signal(getattr(msg, "error", None), " ".join(
                b.text for b in msg.content if isinstance(b, sdk.TextBlock)))
            if signal and self._auth_turn is None:
                self._auth_turn = signal            # the primary signal (a logged-out session)
            recorded = 0
            for block in msg.content:
                if isinstance(block, sdk.TextBlock):
                    self.stream.append("text", {"text": block.text})
                elif isinstance(block, sdk.ToolUseBlock):
                    self.stream.append("tool", {"id": block.id, "name": block.name,
                                                "input": block.input})
                elif isinstance(block, sdk.ThinkingBlock):
                    self.stream.append("thinking", _thinking_payload(block.thinking or ""))
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
            self._note_session(msg.session_id)
        elif isinstance(msg, getattr(sdk, "RateLimitEvent", ())):
            self._on_rate_limit(msg.rate_limit_info)
        else:
            self.stream.append("other", {"type": type(msg).__name__})


class _ScrubbedAuthEnv:
    """os.environ without accounts.AUTH_VARS for the life of the block,
    each put back after. The SDK starts the CLI with {**os.environ,
    **options.env}: an overlay cannot unset a variable, so a key, a token,
    a config dir, a base URL or a provider switch inherited from the
    invoking shell would pick the credentials or the provider (cousin-runner
    removes them for good; a one-off caller such as cousin-migrate gets
    them back afterwards). It edits the whole process's environment: call
    it only from a one-shot process (cousin-migrate, cousin-runner
    --check-auth), never from one with other threads or async work that
    reads os.environ, such as the console or a serving runner."""

    def __enter__(self):
        import os
        self.saved = {k: os.environ.pop(k) for k in accounts.AUTH_VARS if k in os.environ}
        return self

    def __exit__(self, *exc):
        import os
        for key in accounts.AUTH_VARS:
            os.environ.pop(key, None)
        os.environ.update(self.saved)
        return False


# A throwaway session that answers with text only (the review gate's
# reviewer, validate): no transcript, and no MCP server but the ones it is
# given. Without strict-mcp-config the CLI attaches the account's claude.ai
# connectors whatever mcp_servers says, ~42k tokens read on every call.
BARE_SESSION_ARGS = {"no-session-persistence": None, "strict-mcp-config": None}


def validate_account(account, root, *, model=None, effort=None, timeout=90.0,
                     client_factory=None, commit_attribution=True):
    """`cousin-runner --check-auth --validate`: ONE smallest model
    turn on a bare, throwaway client under the account's environment:
    setting_sources=[], no session store, no tools, no MCP server, no
    hooks, max_turns=1, a temporary cwd, a timeout. Never the cousin's own
    SdkRunner: no inbox row, no transcript in sessions.db, nothing mined,
    no usage row. (exit code, line): 0 the turn answered, 4 it did not
    (the account's own words), 2 a configuration error. The account is
    the only source of credentials: every accounts.AUTH_VARS variable of
    the calling process is out of the environment while the turn runs
    (_ScrubbedAuthEnv), for every caller."""
    import shutil
    import tempfile
    sdk = _sdk()
    try:
        env = accounts.account_env(account, root)
    except accounts.SecretMissing as err:
        return 4, "validate: %s" % err
    except accounts.AccountsError as err:
        return 2, "validate: %s" % err
    cwd = tempfile.mkdtemp(prefix="cousin-validate-")
    # no-session-persistence: no transcript under the account's config dir
    options = sdk.ClaudeAgentOptions(cwd=cwd, model=model, effort=effort, env=env,
                                     setting_sources=[], tools=[], mcp_servers={}, max_turns=1,
                                     settings=_attribution_settings(commit_attribution),
                                     extra_args=dict(BARE_SESSION_ARGS))
    factory = client_factory or (lambda o: sdk.ClaudeSDKClient(options=o))

    async def one_turn():
        client = factory(options)
        await client.connect()
        try:
            await client.query("Reply only OK.")
            signal = failed = None
            async for msg in client.receive_response():
                if isinstance(msg, sdk.AssistantMessage):
                    error = getattr(msg, "error", None)
                    signal = signal or auth.assistant_signal(error)
                    if error and failed is None:
                        # any typed error fails the turn, not only auth: a model
                        # the CLI cannot run is an invalid_request 400
                        said = " ".join(getattr(b, "text", "") for b in msg.content or ()).strip()
                        failed = "%s: %s" % (error, said[:300] or error)
                elif isinstance(msg, sdk.SystemMessage) and msg.subtype == "api_retry":
                    retry = auth.retry_signal(msg.data)
                    # a key or token's 401 now is a 401 in 3 minutes; a login
                    # refreshes at the next attempt: read on to the result
                    if retry and not accounts.resume_via_cli(account):
                        return 4, "validate: %s" % retry["detail"]
                elif isinstance(msg, sdk.ResultMessage):
                    signal = signal or auth.result_signal(
                        bool(msg.is_error), getattr(msg, "api_error_status", None),
                        getattr(msg, "result", None), getattr(msg, "errors", None))
                    if not failed and (msg.subtype != "success"
                                       or getattr(msg, "api_error_status", None)):
                        # an API error or a stopped turn that was not flagged is_error
                        failed = "result %s%s" % (msg.subtype, "" if not getattr(
                            msg, "api_error_status", None) else " (HTTP %s)" % msg.api_error_status)
                    if signal or failed or msg.is_error:
                        return 4, "validate: %s" % ((signal or {}).get("detail") or failed
                                                    or msg.result or "an error result")
                    return 0, "validate: ok (one model turn answered)"
            return 4, "validate: the stream ended with no result"
        finally:
            await client.disconnect()

    try:
        with _ScrubbedAuthEnv():
            return asyncio.run(asyncio.wait_for(one_turn(), timeout))
    except asyncio.TimeoutError:
        return 4, "validate: no answer within %.0fs" % timeout
    except Exception as exc:  # noqa: BLE001 - any failure to answer is the answer
        return 4, "validate: %s: %s" % (type(exc).__name__, exc)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
