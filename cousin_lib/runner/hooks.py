"""Hooks in-process (spec, "Hooks in-process"): recording, recall,
checkpoints and the waiting_permission state, as SDK hook callbacks.
A hook never raises into the SDK: the failure becomes a `hook` event.

| event                           | callback                                  |
|---------------------------------|-------------------------------------------|
| PreToolUse (Agent|Task|Bash)    | recorder; its updatedInput output or {}   |
| PostToolUse, PostToolUseFailure | recorder (job close, activity line)       |
| SubagentStop                    | recorder (background agent close)         |
| UserPromptSubmit                | recall -> additionalContext, `recall`     |
| Stop                            | session checkpoint, `checkpoint`          |
| PreCompact                      | pre-compact checkpoint, `checkpoint`      |
| Notification, PermissionRequest | `permission`; running -> waiting_permission |

Callbacks run on the SDK's event loop, which is also the runner's turn
loop: nothing slow runs on it. Recall runs on a worker thread, bounded
by RECALL_BUDGET_S. The runner moves `waiting_permission` back to
`running` when the next SDK message arrives (sdk.py, the turn's message
loop)."""
import asyncio
import inspect

from cousin_lib import recording
from cousin_lib.runner.envelope import CONTEXT_MARK

RECORD_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStop")
PRE_MATCHER = "Agent|Task|Bash"
# The chat server's RECALL_BUDGET_SECONDS: past it the prompt goes on
# without recall and the search finishes on its thread, index warm.
RECALL_BUDGET_S = 4.0


def default_recall(home):
    """`recall(body) -> (context text or None, hit count)`: the chat
    server's gates and line (memory_search.recall_context's parts)."""
    def recall(body):
        from cousin_lib import memory_search
        entries = memory_search.recall_entries(home, body)
        return memory_search.recall_line(entries), len(entries)
    return recall


def callbacks(home, *, slug, root, machine, stream, recall=None, recorder=None,
              checkpoints=None, body_for_prompt=None):
    """The callbacks by hook event name, plain `async def cb(input,
    tool_use_id, context)` functions: no SDK types, so tests drive them
    directly. `build_hooks` wraps them in HookMatchers.

    recall: `recall(body) -> (text or None, hits)`, a blocking callable
    run on a worker thread; text becomes the prompt's additionalContext,
    hits goes on the `recall` event. Default: default_recall(home).
    body_for_prompt: `f(prompt) -> str`, the text to search for a
    submitted prompt (the runner passes the live turn's newest row body,
    not the whole envelope). Default: the prompt itself."""
    from cousin_lib.runner import checkpoints as _cp
    cp = checkpoints or _cp
    recall = recall or default_recall(home)
    recorder = recorder or (lambda payload: recording.handle(payload, home, root, slug=slug))
    body_for_prompt = body_for_prompt or (lambda prompt: prompt)

    def guarded(name, fn):
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
                return {}
        cb.__name__ = name
        return cb

    def record(payload):
        return recorder(dict(payload))

    async def on_prompt(payload):
        prompt = payload.get("prompt") or ""
        if CONTEXT_MARK in prompt:
            # the chat server already recalled for this item
            stream.append("recall", {"hits": 0, "skipped": "context present"})
            return {}
        body = body_for_prompt(prompt) or ""
        try:
            text, n = await asyncio.wait_for(asyncio.to_thread(recall, body), RECALL_BUDGET_S)
        except asyncio.TimeoutError:
            stream.append("recall", {"hits": 0, "timed_out": True})
            return {}
        stream.append("recall", {"hits": n})
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": text}}

    def on_stop(payload):
        path = cp.write_session_checkpoint(home, slug=slug)
        stream.append("checkpoint", {"kind": "session", "path": str(path)})
        return {}

    def on_precompact(payload):
        path = cp.write_pre_compact_checkpoint(home, slug=slug)
        stream.append("checkpoint", {"kind": "pre_compact", "path": str(path)})
        return {"systemMessage": "Context compaction imminent - checkpoint written to %s"
                                 % path.relative_to(home)}

    def waiting(payload):
        event = {"event": payload.get("hook_event_name")}
        for key in ("tool_name", "message"):
            if payload.get(key) is not None:
                event[key] = payload[key]
        stream.append("permission", event)
        if machine.state == "running":
            machine.to("waiting_permission",
                       payload.get("tool_name") or payload.get("message") or "")
        return {}

    table = {ev: guarded(ev, record) for ev in RECORD_EVENTS}
    table["UserPromptSubmit"] = guarded("UserPromptSubmit", on_prompt)
    table["Stop"] = guarded("Stop", on_stop)
    table["PreCompact"] = guarded("PreCompact", on_precompact)
    table["Notification"] = guarded("Notification", waiting)
    table["PermissionRequest"] = guarded("PermissionRequest", waiting)
    return table


def build_hooks(home, **kw):
    """The SDK `hooks` option: HookEvent -> [HookMatcher]. The matcher
    `Agent|Task|Bash` is on PreToolUse only; every other event matches
    all. Imports the SDK here only."""
    from claude_agent_sdk import HookMatcher
    out = {}
    for event, cb in callbacks(home, **kw).items():
        matcher = PRE_MATCHER if event == "PreToolUse" else None
        out[event] = [HookMatcher(matcher=matcher, hooks=[cb])]
    return out
