"""The live turn's tool calls, on disk: what a restart must not repeat.

A runner that dies or is stopped mid-turn leaves its claimed rows to be
delivered again (main.py's sweep, restart_note). A RESUMED session still
has the calls in its transcript, but a call cut in flight has no result
there, and a FRESH session has none of them: it would run the whole turn
again, a `git push` or a sent message included. So the primary runner
writes each tool call of the live turn here as it sees it in the message
stream (sdk._note_tools): one line when the call starts, one when its
result arrives. A turn's `begin` starts the file afresh with the turn's
row ids and first message (never a result: an interrupted turn ends in
one too), and the next start empties it after using it, so what a
restart finds is the last turn's, and the row ids say whether it is the
turn that was cut.

The next start puts `lines()` into the session's first runner line:
what finished, and what started with no result (it may or may not have
happened). What happens to the message decides the wording: a death
requeues the cut turn's rows (main.py's sweep), so the message comes
again; a stop closes them as delivered, so it does not, and the line
quotes it. Read-only tools are left out: repeating them costs nothing."""
import json
import os
from pathlib import Path

FILE = ("data", "turn-tools.jsonl")
# Calls that change nothing: never worth a line in the restart note.
READ_ONLY = frozenset({"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
                       "TodoWrite", "ToolSearch", "BashOutput", "NotebookRead"})
SUMMARY_CHARS = 160
MAX_LINES = 12
BODY_CHARS = 600


def _path(home):
    return Path(home).joinpath(*FILE)


def _summary(name, tool_input):
    """One short line for a call: its command, path or first fields."""
    data = tool_input if isinstance(tool_input, dict) else {}
    for key in ("command", "file_path", "description", "prompt", "text"):
        if data.get(key):
            text = str(data[key])
            break
    else:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True) if data else ""
    text = " ".join(text.split())
    if len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS - 3] + "..."
    return text


def _append(home, record):
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def begin(home, rows):
    """A turn begins: the file starts afresh with its row ids and first
    message, so a restart can tell this turn's calls from an older one's."""
    clear(home)
    body = str((rows[0] or {}).get("body") or "") if rows else ""
    if len(body) > BODY_CHARS:
        body = body[:BODY_CHARS - 3] + "..."
    _append(home, {"event": "turn", "ids": [r.get("id") for r in rows if r], "body": body})


def turn(home):
    """{"ids", "body"} of the turn the file belongs to, or None."""
    try:
        first = _path(home).read_text(encoding="utf-8").split("\n", 1)[0]
        rec = json.loads(first)
    except (FileNotFoundError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("event") == "turn" else None


def started(home, tool_use_id, name, tool_input):
    if name in READ_ONLY:
        return
    _append(home, {"id": tool_use_id, "tool": name,
                   "summary": _summary(name, tool_input), "event": "start"})


def finished(home, tool_use_id, *, error=False):
    _append(home, {"id": tool_use_id, "event": "error" if error else "done"})


def clear(home):
    try:
        os.unlink(_path(home))
    except FileNotFoundError:
        pass


def calls(home):
    """[{"tool", "summary", "state": done|error|open}] in start order; a
    result for a call never started here (a read-only tool) is ignored,
    an unreadable line is skipped."""
    try:
        text = _path(home).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out, by_id = [], {}
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("event") == "start":
            call = {"tool": rec.get("tool"), "summary": rec.get("summary", ""), "state": "open"}
            by_id[rec.get("id")] = call
            out.append(call)
        elif rec.get("id") in by_id:
            by_id[rec["id"]]["state"] = rec.get("event", "done")
    return out


# How to check a call that STARTED and left no result, instead of running
# it again: look at what it would have changed, where it would have
# changed it.
CHECK_HINT = ("To check a NO RESULT call, look at its effect, don't rerun it: after a"
              " `git push`, compare `git ls-remote <remote> <branch>` with your commit;"
              " after a command over ssh, read the remote state over ssh first (the"
              " file, the service, the device) before acting again; after a send or a"
              " reply, read the chat history; after a file write, read the file.")


def lines(home, *, comes_again):
    """The cut turn's calls as text for the restart note, or "" when there
    were none. The last MAX_LINES calls only, with how many came before.
    `comes_again`: the turn's rows were requeued (a death), so its message
    is delivered again; otherwise (a stop) it was closed as delivered, and
    the line quotes it."""
    found = calls(home)
    if not found:
        return ""
    shown = found[-MAX_LINES:]
    word = {"done": "finished", "error": "failed",
            "open": "STARTED, NO RESULT: it may or may not have happened"}
    rows = ["- %s `%s` (%s)" % (c["tool"], c["summary"], word.get(c["state"], c["state"]))
            for c in shown]
    head = "Before the cut, that turn had already run these tool calls (most recent last)."
    if comes_again:
        head += (" The message it was answering is delivered again after this line: do"
                 " not repeat what already ran, and check the state of anything marked"
                 " NO RESULT before running it again.")
    else:
        body = (turn(home) or {}).get("body") or ""
        head += (" That turn's message is not delivered again (a stop closes it as"
                 " delivered). If its work is still due, continue it without repeating"
                 " what already ran, and check the state of anything marked NO RESULT"
                 " first.")
        if body:
            head += " The message was: %s" % json.dumps(body, ensure_ascii=False)
    if len(found) > len(shown):
        head += " (%d earlier calls not shown.)" % (len(found) - len(shown))
    if any(c["state"] == "open" for c in shown):
        head += " " + CHECK_HINT
    return head + "\n" + "\n".join(rows)
