"""Readable logs of what a cousin does, written by its harness hooks.

Nothing here is called by the cousin: cousin_lib.job_hooks calls it
from the agent harness's hook events, so every action is recorded
without the cousin knowing or remembering to.

- The activity log: one line per tool call, success or failure, the
  cousin's own and its subagents', in <home>/data/activity/<date>.log
  (local date). `21:17:03  Bash   ok    git status --short`.
- A subagent's job log: its transcript rendered as text (what it said,
  each tool call, the start of each result), appended to the job's log
  when it finishes. The harness keeps each subagent's transcript under
  <session dir>/subagents/agent-<id>.jsonl beside a .meta.json naming
  the tool call that launched it, which is how a job finds its own.

Stdlib only and best-effort: a hook must never fail the call it
watches, so every function here swallows what it cannot read.
"""
import json
import os
import pathlib
import time
from datetime import datetime

ACTIVITY_DIR = "activity"
LINE_CHARS = 240
RESULT_LINES = 4
RESULT_CHARS = 400


def _one_line(text, limit=LINE_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ------------------------------------------------------------ activity

_WRAPPER_PREFIXES = ("trap ", "exec > >(tee -a ")


def unwrap_command(command):
    """A background shell's command as the cousin wrote it: the job
    hook prepends its own trap and tee lines (job_hooks._wrap), and the
    post-call payload carries the rewritten command."""
    lines = command.split("\n")
    while lines and lines[0].startswith(_WRAPPER_PREFIXES):
        lines.pop(0)
    return "\n".join(lines).strip()


def describe_call(tool, tool_input):
    """What a tool call did, in a few words: the command, the file, the
    pattern. Unknown tools show their input compactly."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    get = tool_input.get
    if tool == "Bash":
        what = unwrap_command(get("command") or "")
        if get("description"):
            what = "%s  # %s" % (_one_line(what, 160), get("description"))
        if get("run_in_background"):
            what = "[bg] " + what
        return what
    if tool in ("Read", "Write", "Edit", "NotebookEdit", "MultiEdit"):
        return get("file_path") or get("notebook_path") or ""
    if tool in ("Grep", "Glob"):
        where = get("path") or ""
        return ("%s in %s" % (get("pattern"), where) if where
                else str(get("pattern") or ""))
    if tool in ("Agent", "Task"):
        return "%s: %s" % (get("subagent_type") or "agent",
                           get("description") or get("prompt") or "")
    if tool in ("WebFetch",):
        return get("url") or ""
    if tool in ("WebSearch",):
        return get("query") or ""
    try:
        return json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(tool_input)


def outcome(event, response, error=None):
    """ok, FAIL or INTR, and for a failure its first words."""
    if event == "PostToolUseFailure":
        return "FAIL", _one_line(error, 120)
    if isinstance(response, dict):
        if response.get("interrupted"):
            return "INTR", ""
        if response.get("is_error") or response.get("isError"):
            return "FAIL", _one_line(response.get("error")
                                     or response.get("stderr"), 120)
    return "ok", ""


def activity_line(payload, *, now=None):
    """The one line an activity log records for a hook payload."""
    event = payload.get("hook_event_name")
    tool = str(payload.get("tool_name") or "?")
    state, why = outcome(event, payload.get("tool_response"),
                         payload.get("error"))
    who = ""
    agent = payload.get("agent_type") or payload.get("agent_id")
    if agent:
        who = "[%s] " % agent
    what = _one_line(describe_call(tool, payload.get("tool_input")))
    if why:
        what = "%s  -> %s" % (what, why)
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%H:%M:%S")
    return "%s  %-12s %-4s  %s%s" % (stamp, tool[:12], state, who, what)


def activity_path(home, *, now=None):
    day = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")
    return pathlib.Path(home) / "data" / ACTIVITY_DIR / ("%s.log" % day)


def record(home, payload, *, now=None):
    """Append the payload's line to today's activity log."""
    path = activity_path(home, now=now)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(activity_line(payload, now=now) + "\n")
    return path


# ----------------------------------------------------- subagent job logs

def subagent_dirs(payload):
    """Where the harness keeps this session's subagent transcripts:
    <transcript dir>/<session id>/subagents."""
    parent = payload.get("transcript_path")
    session = payload.get("session_id")
    if not parent or not session:
        return []
    return [pathlib.Path(parent).parent / session / "subagents"]


def find_subagent_transcript(payload, tool_use_id):
    """The transcript of the subagent that tool call `tool_use_id`
    launched, found through the .meta.json beside it; None when the
    harness left none."""
    direct = payload.get("agent_transcript_path")
    if direct and pathlib.Path(direct).is_file():
        return pathlib.Path(direct)
    if not tool_use_id:
        return None
    for folder in subagent_dirs(payload):
        try:
            metas = sorted(folder.glob("agent-*.meta.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for meta in metas:
            try:
                data = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if data.get("toolUseId") == tool_use_id:
                jsonl = meta.with_name(meta.name[:-len(".meta.json")]
                                       + ".jsonl")
                if jsonl.is_file():
                    return jsonl
    return None


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _stamp(entry):
    ts = entry.get("timestamp") or ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) \
            .astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"


def render_transcript(path):
    """A subagent transcript as readable text: its words, each tool
    call on one line, the first lines of each result."""
    out = []
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError as err:
        return "(transcript unreadable: %s)\n" % err
    first_user = True
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        kind = entry.get("type")
        if kind == "user" and isinstance(content, str):
            if first_user:
                first_user = False  # the prompt; the header has it
                continue
            out.append("%s  > %s" % (_stamp(entry), _one_line(content)))
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if kind == "assistant" and btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    out.append("%s  %s" % (_stamp(entry), text))
            elif kind == "assistant" and btype == "tool_use":
                out.append("%s  -> %s: %s" % (
                    _stamp(entry), block.get("name"),
                    _one_line(describe_call(block.get("name"),
                                            block.get("input")))))
            elif kind == "user" and btype == "tool_result":
                text = _result_text(block.get("content")).strip()
                if block.get("is_error"):
                    text = "ERROR " + text
                if text:
                    snippet = text[:RESULT_CHARS].splitlines()[:RESULT_LINES]
                    out.extend("            | %s" % s for s in snippet)
    return "\n".join(out) + "\n"


def write_header(log_path, *, title, kind, detail):
    """The first lines of a job log, written when the job starts."""
    path = pathlib.Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("# %s: %s\n# started %s\n\n%s\n\n" % (
            kind, title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            detail.rstrip()))


def append_transcript(log_path, transcript, *, status, summary):
    """The rendered transcript and the outcome, when a subagent ends."""
    with open(log_path, "a", encoding="utf-8") as fh:
        if transcript is not None:
            fh.write("## transcript (%s)\n\n" % os.path.basename(transcript))
            fh.write(render_transcript(transcript))
        else:
            fh.write("(the harness left no transcript for this agent)\n")
        fh.write("\n## %s %s\n%s\n" % (
            status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            (summary or "").strip()))
