"""Hooks in-process (spec, "Hooks in-process"): recording, recall,
checkpoints and the waiting_permission state, as SDK hook callbacks.
A hook never raises into the SDK: the failure becomes a `hook` event.

| event                           | callback                                  |
|---------------------------------|-------------------------------------------|
| PreToolUse:policy (no matcher)  | memory perimeter (a background pass),    |
|                                 | policy.toml deny/ask, `policy`;           |
|                                 | ask -> deny                               |
| PreToolUse (Agent|Task|Bash)    | recorder, only when the policy allows;    |
|                                 | its updatedInput output or {}             |
| PostToolUse, PostToolUseFailure | recorder (job close, activity line)       |
| SubagentStop                    | recorder (background agent close)         |
| UserPromptSubmit                | recall -> additionalContext, `recall`     |
| Stop                            | session checkpoint, `checkpoint`; the     |
|                                 | reply gate (`gate` event, may block once) |
| PreCompact                      | pre-compact checkpoint, `checkpoint`;     |
|                                 | then a rollover request (request_rollover) |
| Notification, PermissionRequest | `permission`; running -> waiting_permission |

The CLI runs every PreToolUse callback that matches a call
concurrently, and a deny from any of them wins by aggregation: list
order sequences nothing. So the policy callback cannot run "before" the
recorder, and the recorder checks the policy itself (`gate`, the same
decision the policy callback returns) and records nothing for a call
the policy denies or asks about. The hooks fire inside a subagent's
turn too (the input carries `agent_id`); a subagent's `reply` with no
`thread` is denied, because the live turn's implicit thread is the
parent's, not the subagent's.

Callbacks run on the SDK's event loop, which is also the runner's turn
loop: nothing slow runs on it. Recall, the recorder and the checkpoint
writes run on worker threads (asyncio.to_thread); recall is bounded by
RECALL_BUDGET_S. The runner moves `waiting_permission` back to
`running` when the next SDK message arrives (sdk.py, the turn's message
loop)."""
import asyncio
import contextlib
import functools
import inspect
import os
import threading

from cousin_lib import perimeter, recording
from cousin_lib.runner.envelope import CONTEXT_MARK

RECORD_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStop")
PRE_MATCHER = "Agent|Task|Bash"
# The recall budget: past it the prompt goes on
# without recall and the search finishes on its thread, index warm.
RECALL_BUDGET_S = 4.0
# The recorder writes <root>/data/jobs.db, which every cousin on the host
# shares (sqlite waits up to 5 s per connect, and a call connects more than
# once), and the CLI holds the tool until the hook answers: past this
# budget the hook answers without it and says so. A PreToolUse's late
# recording is cancelled (the call runs unrecorded, and no row outlives it);
# a later event's finishes on its thread when the store frees.
RECORD_BUDGET_S = 2.0
PERMISSION_NOTIFICATION = "permission_prompt"
# The reply tool as the CLI names it: the runner registers its tool server
# under the key "cousin" (sdk.py, options()).
REPLY_TOOL = "mcp__cousin__reply"
SUBAGENT_REPLY_REASON = "a subagent must name the thread it answers"
# The thread kinds where a person waits on the chat surface: a turn on one
# that ends without `reply` is sent back once by the reply gate (here on
# the sdk lane, in opencode.py's _on_idle on the opencode lane).
HUMAN_KINDS = ("operator", "person")
REPLY_GATE_REASON = (
    "You are ending this turn without calling reply on %s, so the person who"
    " wrote sees nothing on the chat surface. If an answer is due, call reply"
    " now. If none is (an acknowledgement, a message that needs no answer),"
    " just end the turn again: this check runs once per turn.")


def _kind(thread):
    return str(thread).partition(":")[0]


def unanswered_threads(live, replied, implicit):
    """The reply gate's decision, the same on every lane that has one: the
    live operator or person threads (`live`, thread ids in turn order) no
    successful reply this turn covered. `replied` holds the thread ids a
    reply named, lowercased; `implicit` is whether a reply named none,
    which covers the turn only while exactly one such thread is live."""
    human = [t for t in live if _kind(t) in HUMAN_KINDS]
    if implicit and len(human) == 1:
        return []
    return [t for t in human if str(t).lower() not in replied]


def reply_gate_reason(missing):
    """The send-back text for the unanswered threads `missing`."""
    return REPLY_GATE_REASON % " and ".join(missing)


class ReplyLedger:
    """The reply gate's state for the turn in flight: the threads a
    successful reply named (lowercased), and whether one named none (the
    implicit thread). Each lane feeds it its own way (the sdk lane from
    PostToolUse, the opencode lane from a completed `cousin_reply` part
    of the session's own event stream); neither feeds a subagent's."""

    def __init__(self):
        self.threads = set()
        self.implicit = False

    def note(self, tool_input):
        """One successful `reply` call with these arguments."""
        thread = tool_input.get("thread") if isinstance(tool_input, dict) else None
        if str(thread or "").strip():
            self.threads.add(str(thread).strip().lower())
        else:
            self.implicit = True

    def prompted(self, thread=None):
        """A new prompt is answered afresh: a reply sent before it does not
        cover its thread `thread`; the other threads keep what they were
        answered. With no thread (a prompt nobody can place) every thread
        is reset."""
        if thread is None:
            self.clear()
            return
        self.threads.discard(str(thread).lower())
        if _kind(thread) in HUMAN_KINDS:
            self.implicit = False

    def clear(self):
        self.threads.clear()
        self.implicit = False

    def unanswered(self, live):
        return unanswered_threads(live, self.threads, self.implicit)


def gate(policy, payload, *, root=None, home=None, slug=None):
    """The PreToolUse decision for one hook payload, `(decision,
    reason)`: the memory perimeter for a background pass, then a
    subagent's reply that names no thread, then policy.toml decides. The
    policy callback enforces it and the recorder consults it, so the two
    never disagree.

    The perimeter applies when the payload carries `agent_id`: that is a
    subagent. #166's background pass MUST run as one, or call
    perimeter.assert_writable at its own writers - the pass is not designed
    yet, so that is a requirement on that design and not a fact about it.
    The primary session keeps its operator-directed edits, because editing
    the law and committing a portrait is the operator's own work and is
    routine. (policy.toml can deny Write and Edit only as whole tools, and
    Bash commands by pattern; it has no per-path rule.) With `root`, only
    paths under the framework root are protected, so a subagent working in
    another repository is never refused its own files. `home` and `slug`
    are the cousin whose session this is (hooks.callbacks passes its
    own): the perimeter's rows that depend on who writes, its own home,
    another cousin's and its own proposals, read them. Reads are never
    refused: a subagent reads the law and the shared rules as a matter
    of course. The deny is per call and names the path, so it reads as a
    boundary rather than a failure."""
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if payload.get("agent_id"):
        refusal = perimeter.check_tool(tool_name, tool_input, root=root,
                                       cwd=payload.get("cwd"), home=home, slug=slug)
        if refusal:
            return "deny", refusal
        if tool_name == REPLY_TOOL:
            thread = tool_input.get("thread") if isinstance(tool_input, dict) else None
            if not str(thread or "").strip():
                return "deny", SUBAGENT_REPLY_REASON
    return policy.decide(tool_name, tool_input)


def default_recall(home, root=None):
    """`recall(body) -> (context text or None, hit count)`: the recall
    gates and line (memory_search.recall_context's parts),
    reading the runner's `root`, never the environment's."""
    def recall(body):
        from cousin_lib import memory_search
        entries = memory_search.recall_entries(home, body, root=root)
        return memory_search.recall_line(entries), len(entries)
    return recall


def _accepts(fn, name):
    """fn takes a keyword argument `name` (an injected recorder may not)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD
                                 for p in params.values())


def callbacks(home, *, slug, root, machine, stream, recall=None, recorder=None,
              checkpoints=None, body_for_prompt=None, lock=None, policy=None,
              request_rollover=None, live_threads=None, reply_gate=True,
              watch=None, policy_changed=None, thread_for_prompt=None,
              memory_watch=None):
    """The callbacks by hook event name, plain `async def cb(input,
    tool_use_id, context)` functions: no SDK types, so tests drive them
    directly. `build_hooks` wraps them in HookMatchers.

    recall: `recall(body) -> (text or None, hits)`, a blocking callable
    run on a worker thread; text becomes the prompt's additionalContext,
    hits goes on the `recall` event. Default: default_recall(home, root).
    body_for_prompt: `f(prompt) -> str`, the text to search for a
    submitted prompt (the runner passes the body of the row whose
    envelope matches the prompt, "" for a thread that is not operator
    or person chat, never the whole envelope). Default: the prompt itself.
    lock: the runner's state lock (a threading.Lock); the permission
    move checks and transitions under it. Never held across an await.
    policy: a `policy.Policy`; when given, the table gains
    `"PreToolUse:policy"`, a no-matcher PreToolUse callback enforcing
    `gate`, and the recorder's PreToolUse step records only a call
    `gate` allows (the CLI runs both callbacks concurrently, so neither
    can count on the other having run). `build_hooks` splits that key
    on `":"` and puts it at the head of the event's matcher list.
    request_rollover: a callable(reason); PreCompact calls it once, after
    its checkpoint, so a compaction the CLI is about to do becomes a
    rollover at the next turn boundary. Run on a worker thread.
    live_threads: `f() -> tuple of thread ids` the live turn answers
    (turn.snapshot's threads); with `reply_gate` (cousin.toml [agent]
    reply_gate, default on) a Stop whose live operator or person
    threads got no successful `reply` this turn is blocked once with
    REPLY_GATE_REASON. The CLI marks the Stop that follows a block
    `stop_hook_active`, and that one always passes, so the gate costs at
    most one more model step and can never loop.
    memory_watch: a memory_watch.MemoryWatch; each submitted prompt
    checks it, and memory written from outside the session (the console,
    the review gate, dreaming) reaches the model as a runner note after
    the config note and before recall. Per runner, like `watch`.
    thread_for_prompt: `f(prompt) -> thread id or None`; a submitted
    prompt resets the answered state of its own thread only, so a peer's
    message folded into an operator turn does not undo the operator's
    reply. With no such function, or a prompt it cannot place, every
    thread is reset.
    watch: a config_watch.ConfigWatch; each submitted prompt checks it,
    and a change reaches the model as a runner note in that prompt's
    additionalContext (ahead of recall). A policy.toml edit tightens the
    live policy at once (Policy.tightened_by), never loosens it.
    policy_changed: a callable(policy) told each time `watch` tightened the
    live policy; the runner keeps it (its next connect's hooks start from
    it, and its tools read the outbound filter from it). Pass the watch
    and keep the policy per runner, not per connect: a fresh watch's
    baseline is the already-edited file, so it would see no change."""
    from cousin_lib.runner import checkpoints as _cp
    cp = checkpoints or _cp
    recall = recall or default_recall(home, root)
    recorder = recorder or (lambda payload, cancelled=None: recording.handle(
        payload, home, root, slug=slug, cancelled=cancelled))
    takes_cancel = _accepts(recorder, "cancelled")
    body_for_prompt = body_for_prompt or (lambda prompt: prompt)
    lock = lock if lock is not None else contextlib.nullcontext()
    # the policy the gate enforces: the start's, tightened by any edit since
    live = {"policy": policy}
    # The reply gate's state for the turn in flight
    replied = ReplyLedger()

    def note_reply(payload):
        if payload.get("tool_name") != REPLY_TOOL or payload.get("agent_id"):
            return
        replied.note(payload.get("tool_input"))

    def unanswered():
        """The live operator/person threads no reply covered this turn."""
        if not reply_gate or live_threads is None:
            return []
        return replied.unanswered(live_threads())

    def guarded(name, fn, fail_closed=False):
        """Every hook fails open but the gate: an exception becomes a
        `hook` error event and `{}`. `fail_closed` is for a gate, not a
        side effect - the policy callback is the only caller today: on
        top of the `hook` event it also appends a `policy` deny event
        and returns a deny `hookSpecificOutput`, so a bug in the policy
        check cannot silently allow the tool."""
        is_async = inspect.iscoroutinefunction(fn)

        async def cb(hook_input, tool_use_id, context):
            try:
                out = await fn(hook_input) if is_async else fn(hook_input)
                return out if out else {}
            except Exception as err:  # noqa: BLE001 - never into the SDK
                try:
                    stream.append("hook", {"event": name,
                                           "error": "%s: %s" % (type(err).__name__, err)})
                except Exception:  # noqa: BLE001 - the stream itself failed
                    pass
                if not fail_closed:
                    return {}
                reason = "policy check failed: %s: %s" % (type(err).__name__, err)
                try:
                    stream.append("policy", {"tool": hook_input.get("tool_name"),
                                             "decision": "deny", "reason": reason})
                except Exception:  # noqa: BLE001 - the stream itself failed
                    pass
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "permissionDecision": "deny",
                                               "permissionDecisionReason": reason}}
        cb.__name__ = name
        return cb

    def recorder_for(event):
        async def record(payload):
            if event == "PostToolUse":
                note_reply(payload)
            # A call the policy denies never runs: no job row, no rewrite.
            if event == "PreToolUse" and policy is not None \
                    and gate(live["policy"], payload, root=root, home=home, slug=slug)[0] != "allow":
                return {}
            # The recorder checks `cancelled` before it registers a row: once
            # the hook has answered without it, a late row would be one nobody
            # closes (no rewrite, no trap), so the late recording is dropped.
            cancelled = threading.Event()
            kwargs = {"cancelled": cancelled} if takes_cancel else {}
            work = asyncio.get_running_loop().run_in_executor(
                None, functools.partial(recorder, dict(payload), **kwargs))
            try:
                # shielded: past the budget the work runs on, and its end is seen
                return await asyncio.wait_for(asyncio.shield(work), RECORD_BUDGET_S)
            except asyncio.CancelledError:
                # the SDK cancelled the hook: nobody takes this answer either
                cancelled.set()
                work.add_done_callback(functools.partial(late, event))
                raise
            except asyncio.TimeoutError:
                cancelled.set()
                work.add_done_callback(functools.partial(late, event))
                if event == "PreToolUse":
                    missed = "the call runs without its job row or rewrite"
                else:
                    missed = "the hook answered; the job row is closed when the store frees"
                stream.append("hook", {"event": event, "error": (
                    "recorder over its %.1fs budget (a shared store busy): %s"
                    % (RECORD_BUDGET_S, missed))})
                return {}
        return record

    def late(event, work):
        """A recording that finished after its hook answered: nobody awaits
        it, so its failure is written here instead of lost."""
        if work.cancelled():
            return
        err = work.exception()
        if err is not None:
            try:
                stream.append("hook", {"event": event, "error": "late recorder: %s: %s"
                                       % (type(err).__name__, err)})
            except Exception:  # noqa: BLE001 - the stream itself failed
                pass

    async def config_note():
        """The runner note for config changed since the last prompt, or
        "". A policy.toml change tightens `live` here."""
        if watch is None:
            return ""
        changes = await asyncio.to_thread(watch.check)
        if not changes:
            return ""
        from cousin_lib.runner import config_watch
        from cousin_lib.runner.policy import FILE, Policy
        current = live["policy"] if live["policy"] is not None else Policy()
        text, tightened = config_watch.note(
            changes, current, lambda t: Policy.parse(t, source=os.path.join(str(home), FILE)))
        if live["policy"] is not None and tightened is not live["policy"]:
            live["policy"] = tightened
            if policy_changed is not None:
                policy_changed(tightened)
        stream.append("config_change", {"files": [name for name, _, _ in changes]})
        return text

    async def memory_note():
        """The runner note for memory written from outside since the last
        prompt, or ""."""
        if memory_watch is None:
            return ""
        entries = await asyncio.to_thread(memory_watch.check)
        if not entries:
            return ""
        from cousin_lib.runner import memory_watch as mw
        stream.append("memory_update", {"entries": len(entries)})
        return mw.note(entries)

    async def recall_text(prompt):
        if CONTEXT_MARK in prompt:
            # the item already carries its recall context
            stream.append("recall", {"hits": 0, "skipped": "context present"})
            return ""
        body = body_for_prompt(prompt) or ""
        if not body.strip():
            # nothing to search for: a peer, loop or schedule row
            stream.append("recall", {"hits": 0, "skipped": "empty body"})
            return ""
        try:
            text, n = await asyncio.wait_for(asyncio.to_thread(recall, body), RECALL_BUDGET_S)
        except asyncio.TimeoutError:
            stream.append("recall", {"hits": 0, "timed_out": True})
            return ""
        stream.append("recall", {"hits": n})
        return text or ""

    async def on_prompt(payload):
        # a new prompt (a turn's first row or one folded in) is answered
        # afresh: a reply sent before it does not cover its thread; the
        # other threads keep what they were answered
        own = thread_for_prompt(payload.get("prompt") or "") if thread_for_prompt else None
        replied.prompted(own)
        prompt = payload.get("prompt") or ""
        try:
            changed = await config_note()
        except Exception as err:  # noqa: BLE001 - a broken check must not cost recall
            changed = ""
            stream.append("hook", {"event": "UserPromptSubmit",
                                   "error": "config check: %s: %s" % (type(err).__name__, err)})
        try:
            written = await memory_note()
        except Exception as err:  # noqa: BLE001 - a broken check must not cost recall
            written = ""
            stream.append("hook", {"event": "UserPromptSubmit",
                                   "error": "memory check: %s: %s" % (type(err).__name__, err)})
        text = "\n\n".join(t for t in (changed, written, await recall_text(prompt)) if t)
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": text}}

    async def on_stop(payload):
        path = await asyncio.to_thread(cp.write_session_checkpoint, home, slug=slug)
        stream.append("checkpoint", {"kind": "session", "path": str(path)})
        missing = [] if payload.get("stop_hook_active") else unanswered()
        if missing:
            stream.append("gate", {"gate": "reply", "threads": missing})
            return {"decision": "block", "reason": reply_gate_reason(missing)}
        replied.clear()
        return {}

    async def on_precompact(payload):
        # this session's own stream only: a side session's checkpoint must
        # not carry the primary's operator rows, nor the reverse
        path = await asyncio.to_thread(cp.write_pre_compact_checkpoint, home, slug=slug,
                                       stream_path=getattr(stream, "path", None))
        stream.append("checkpoint", {"kind": "pre_compact", "path": str(path)})
        if request_rollover is not None:
            await asyncio.to_thread(request_rollover, "pre-compact")
        return {"systemMessage": "Context compaction imminent - checkpoint written to %s"
                                 % path.relative_to(home)}

    def on_policy(payload):
        decision, reason = gate(live["policy"], payload, root=root, home=home, slug=slug)
        if decision == "allow":
            return {}
        event = {"tool": payload.get("tool_name"), "decision": decision, "reason": reason}
        if payload.get("agent_id"):
            event["agent_id"] = payload["agent_id"]
        stream.append("policy", event)
        if decision == "ask":
            reason += " (no operator approval surface yet: ask is enforced as deny)"
            with lock:
                if machine.state == "running":
                    machine.to("waiting_permission", reason)
                    machine.to("running", "ask enforced as deny")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    def waiting(payload):
        event = {"event": payload.get("hook_event_name")}
        for key in ("tool_name", "message"):
            if payload.get(key) is not None:
                event[key] = payload[key]
        stream.append("permission", event)
        if event["event"] == "Notification" and \
                payload.get("notification_type") != PERMISSION_NOTIFICATION:
            return {}
        with lock:
            if machine.state == "running":
                machine.to("waiting_permission",
                           payload.get("tool_name") or payload.get("message") or "")
        return {}

    table = {ev: guarded(ev, recorder_for(ev)) for ev in RECORD_EVENTS}
    table["UserPromptSubmit"] = guarded("UserPromptSubmit", on_prompt)
    table["Stop"] = guarded("Stop", on_stop)
    table["PreCompact"] = guarded("PreCompact", on_precompact)
    table["Notification"] = guarded("Notification", waiting)
    table["PermissionRequest"] = guarded("PermissionRequest", waiting)
    if policy is not None:
        table["PreToolUse:policy"] = guarded("PreToolUse:policy", on_policy, fail_closed=True)
    return table


def build_hooks(home, **kw):
    """The SDK `hooks` option: HookEvent -> [HookMatcher]. The matcher
    `Agent|Task|Bash` is on PreToolUse only; every other event matches
    all. A key with a `:` (only `"PreToolUse:policy"` today) is split
    on the colon, and its no-matcher HookMatcher is put at the head of
    that event's list; the split key itself never appears in the
    returned table. The position orders nothing: the CLI runs every
    matched PreToolUse callback concurrently and a deny wins by
    aggregation, which is why the recorder checks the policy itself.
    Imports the SDK here only."""
    from claude_agent_sdk import HookMatcher
    out = {}
    for key, cb in callbacks(home, **kw).items():
        event, _, _tag = key.partition(":")
        matcher = PRE_MATCHER if event == "PreToolUse" and not _tag else None
        entry = HookMatcher(matcher=matcher, hooks=[cb])
        if _tag:
            out.setdefault(event, []).insert(0, entry)
        else:
            out.setdefault(event, []).append(entry)
    return out
