"""Tool calls that failed or looked wrong, one row each, kept so they can
be read days later.

The stream holds every event but no row of it says who made a call: a
subagent's calls read as the cousin's own. This log keeps only the odd
calls, each with its agent, its model and the stream it came from, in
data/tool-faults.jsonl. The sdk kind shows it each call and each result
(sdk._record, a subagent's included); the console reads it across the
fleet (routes_tool_faults, the System page's tool faults tab).

A row's kind says what was odd:
  unknown_tool  the tool name does not exist
  misnamed      a ToolSearch call sent under another tool's name
  bad_input     the arguments did not pass the tool's schema
  failed        any other call that came back as an error
  long_wait     a foreground shell wait that held the caller a minute or more

A call cut with no result leaves no row. The file rolls at MAX_BYTES to
`<file>.1`, so a storm of failures cannot fill the disk; `read` takes both."""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cousin_lib import jsonl
from cousin_lib.runner.tool_ledger import _summary

FILE = ("data", "tool-faults.jsonl")
KINDS = ("unknown_tool", "misnamed", "bad_input", "failed", "long_wait")
LONG_WAIT_SECONDS = 60
LONG_SLEEP_SECONDS = 30
WAIT_CHARS = 4096       # of a command, enough for any wait: the search runs on the event loop
ERROR_CHARS = 300
MAX_KEYS = 12
MAX_OPEN = 1000         # calls with no result yet; an interrupted one never gets one
MAX_BYTES = 16 * 1024 * 1024
# The CLI's own refusal opens with this. A failed command's output can quote
# the same words, so only a refusal is read for them.
REFUSAL = "<tool_use_error>"
# `sleep` where a command starts: `time.sleep` or the word in a message is not one.
_SLEEP = re.compile(r"(?:^|[;&|(\n]|\bdo\b|\bthen\b)\s*sleep\s+(\d+)")
_LOOP = re.compile(r"\bdo\b")


def path(home):
    return Path(home).joinpath(*FILE)


def _rolled(target):
    return target.with_name(target.name + ".1")


def _stamp(when):
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _waits(command):
    """A command that waits: a sleep inside a loop, or one long sleep. A short
    sleep before real work (`sleep 5 && make test`) is not a wait."""
    text = str(command)[:WAIT_CHARS]
    return any(int(m.group(1)) >= LONG_SLEEP_SECONDS or _LOOP.search(text, 0, m.end())
               for m in _SLEEP.finditer(text))


def classify(name, tool_input, is_error, text, seconds):
    """The call's kind, None for an ordinary one. A wait is told first: one
    that hits the tool's time limit also comes back as an error."""
    data = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash" and seconds >= LONG_WAIT_SECONDS \
            and not data.get("run_in_background") and _waits(data.get("command", "")):
        return "long_wait"
    if not is_error:
        return None
    if text.startswith(REFUSAL):
        if name != "ToolSearch" and str(data.get("query", "")).startswith("select:"):
            return "misnamed"
        if "No such tool available" in text:
            return "unknown_tool"
        if "InputValidationError" in text:
            return "bad_input"
    return "failed"


def append(home, row):
    target = path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size >= MAX_BYTES:
        os.replace(target, _rolled(target))
    if not target.exists():     # a failed command's output can hold anything
        os.close(os.open(target, os.O_CREAT | os.O_WRONLY, 0o600))
    jsonl.append_line(target, json.dumps(row, ensure_ascii=False))


def read(home, *, since=None):
    """The rows at or after `since` (a row's own `ts` form), oldest first."""
    rows = []
    target = path(home)
    for source in (_rolled(target), target):
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (since is None or str(row.get("ts", "")) >= since):
                rows.append(row)
    return rows


def report(homes, *, days=7, kind=None, limit=200, now=None):
    """`homes` by slug: each one's counts by kind over the last `days`, and
    the newest `limit` rows of `kind` (every kind when None), `cousin` added."""
    start = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc) \
        - timedelta(days=days)
    since = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    totals, rows = {}, []
    for slug, home in homes.items():
        for row in read(home, since=since):
            counts = totals.setdefault(slug, {})
            counts[row.get("kind")] = counts.get(row.get("kind"), 0) + 1
            if kind is None or row.get("kind") == kind:
                rows.append(dict(row, cousin=slug))
    rows.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return {"days": days, "kinds": list(KINDS), "totals": totals,
            "matched": len(rows), "rows": rows[:limit]}


class Watch:
    """One runner's calls with no result yet, and the row a result makes."""

    def __init__(self, home, stream_id):
        self.home = home
        self.stream_id = stream_id
        self._open = {}

    def started(self, tool_id, name, tool_input, *, model=None, parent=None, now=None):
        if len(self._open) >= MAX_OPEN:
            self._open.pop(next(iter(self._open)))
        self._open[tool_id] = (name, tool_input, model, parent,
                               time.time() if now is None else now)

    def finished(self, tool_id, is_error, text, *, parent=None, now=None):
        """The row for this result, written, or None for an ordinary call.
        A result whose call was not seen (a restart between them) still
        counts when it is an error, with the `parent` its message carries."""
        name, tool_input, model, seen_parent, began = self._open.pop(
            tool_id, (None, None, None, None, None))
        parent = seen_parent or parent
        now = time.time() if now is None else now
        seconds = now - began if began is not None else 0
        kind = classify(name, tool_input, is_error, text, seconds)
        if kind is None:
            return None
        data = tool_input if isinstance(tool_input, dict) else {}
        row = {"ts": _stamp(now), "kind": kind, "tool": name,
               "agent": "subagent" if parent else "main", "parent": parent,
               "model": model, "stream": self.stream_id, "id": tool_id,
               "seconds": round(seconds), "input": _summary(name, tool_input),
               "keys": sorted(data)[:MAX_KEYS],
               "error": text[:ERROR_CHARS] if is_error else None}
        append(self.home, row)
        return row
