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

The runner moves `waiting_permission` back to `running` when the next
SDK message arrives (sdk.py, the turn's message loop)."""
import functools

from cousin_lib import recording

RECORD_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStop")
PRE_MATCHER = "Agent|Task|Bash"
RECALL_TOP = 3


def default_recall(home):
    """`recall(prompt) -> (context text or None, hit count)` over the
    cousin's own memory (memory_search.search, keyword leg at least)."""
    def recall(prompt):
        from cousin_lib import memory_search
        hits, _notice = memory_search.search(prompt, top=RECALL_TOP, home=home)
        if not hits:
            return None, 0
        names = "; ".join(str(h.get("path", "")).rsplit("/", 1)[-1] for h in hits)
        return ("[fw-recall] possibly relevant from your memory: %s - cousin-memory search"
                " for details; ignore if not." % names), len(hits)
    return recall


def callbacks(home, *, slug, root, machine, stream, recall=None, recorder=None,
              checkpoints=None):
    """The callbacks by hook event name, plain `async def cb(input,
    tool_use_id, context)` functions: no SDK types, so tests drive them
    directly. `build_hooks` wraps them in HookMatchers."""
    from cousin_lib.runner import checkpoints as _cp
    cp = checkpoints or _cp
    recall = recall or default_recall(home)
    recorder = recorder or (lambda payload: recording.handle(payload, home, root, slug=slug))

    def guarded(name, fn):
        @functools.wraps(fn)
        async def cb(hook_input, tool_use_id, context):
            try:
                out = fn(hook_input)
                return out if out else {}
            except Exception as err:  # noqa: BLE001 - never into the SDK
                try:
                    stream.append("hook", {"event": name,
                                           "error": "%s: %s" % (type(err).__name__, err)})
                except Exception:  # noqa: BLE001 - the stream itself failed
                    pass
                return {}
        return cb

    def record(payload):
        return recorder(dict(payload))

    def on_prompt(payload):
        text, n = recall(payload.get("prompt") or "")
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
