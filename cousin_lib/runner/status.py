"""A runner cousin's state, read by a process that holds no runner object
(the console's fleet view, cousin-watch): from the cousin's own stores,
never from a port.

- alive: a runner holds `<home>/run/runner.lock` (main.is_running);
- state: the `to` of the last `state` event in the primary event stream
  (`data/stream/<session>.jsonl`, one file per runner process; see
  primary_stream), with `since`, its timestamp;
- kind, pid, unsupported: the `runner` event cousin-runner appends when it
  starts, before the runner's first state change, so it sits at the head
  of that file (the contract items the runner DECLARES unsupported, spec
  "The compatibility layer": visible in the console).

The head is read once; the last state is found by reading the file
backwards a chunk at a time and stopping at the first `state` event, so
a turn that wrote megabytes after its `running` is still read right and
an idle file costs one chunk. A stream the runner of a dead process left
behind still names its last state: `alive` is what says whether
anything is running now.
"""
import json
import time
from pathlib import Path

HEAD_BYTES = 64 * 1024
CHUNK_BYTES = 64 * 1024


def _head_kind(path):
    """The kind of a stream file's first event, or None (empty, unreadable,
    a first line not yet complete)."""
    try:
        with open(path, "rb") as fh:
            line = fh.readline(HEAD_BYTES)
    except OSError:
        return None
    if not line.endswith(b"\n"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event.get("kind") if isinstance(event, dict) else None


def primary_stream(home):
    """The runner's primary stream: the newest data/stream/*.jsonl whose
    first event is `runner` (cousin-runner writes it before anything
    else). Phase 8's side-session streams are headed `side_session`, never
    `runner`, so a busier, newer side stream never wins. Only a home where
    no file has a `runner` head (streams from before 1.14) falls back to
    the newest file. None when there is no stream."""
    base = Path(home) / "data" / "stream"
    found = []
    try:
        for path in base.glob("*.jsonl"):
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:           # removed between the glob and the stat
                continue
    except OSError:
        return None
    found.sort(reverse=True)
    for _mtime, path in found:
        if _head_kind(path) == "runner":
            return path
    return found[0][1] if found else None


def _events(lines):
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _head(path):
    with open(path, "rb") as fh:
        data = fh.read(HEAD_BYTES)
    lines = data.decode("utf-8", "replace").split("\n")
    return lines[:-1]                  # the last piece may be cut mid-line


def _backwards(path):
    """(byte offset, text) of the file's lines, last first, read CHUNK_BYTES
    at a time; the offset is where the line starts."""
    with open(path, "rb") as fh:
        pos = fh.seek(0, 2)
        rest = b""
        while pos > 0:
            step = min(CHUNK_BYTES, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + rest
            lines = data.split(b"\n")
            starts, at = [], pos
            for line in lines:
                starts.append(at)
                at += len(line) + 1
            rest = lines[0]                 # may continue in the chunk before
            for k in range(len(lines) - 1, 0, -1):
                if lines[k]:
                    yield starts[k], lines[k].decode("utf-8", "replace")
        if rest:
            yield 0, rest.decode("utf-8", "replace")


def _offset(path, *, tail=None, after=None):
    """Where to start reading: past the event numbered `after` (the first
    event after it), else before the last `tail` events; 0 for the whole
    file (tail None or 0, or a file shorter than that)."""
    if after is None and not tail:
        return 0
    count = 0
    for start, text in _backwards(path):
        try:
            event = json.loads(text)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if after is not None:
            if int(event.get("seq") or 0) <= after:
                return start + len(text.encode("utf-8", "replace")) + 1
        else:
            count += 1
            if count == tail:
                return start
    return 0


def status(home):
    """{"alive", "state", "since", "session", "kind", "pid", "unsupported"}.
    Everything but `alive` is None (unsupported: []) when no stream exists."""
    from cousin_lib.runner.main import is_running
    out = {"alive": is_running(home), "state": None, "since": None, "session": None,
           "kind": None, "pid": None, "unsupported": []}
    path = primary_stream(home)
    if path is None:
        return out
    out["session"] = path.stem
    try:
        for event in _events(_head(path)):
            if event.get("kind") == "runner":
                payload = event.get("payload") or {}
                out.update(kind=payload.get("kind"), pid=payload.get("pid"),
                           unsupported=list(payload.get("unsupported") or []))
                break
        for event in _events(text for _start, text in _backwards(path)):
            if event.get("kind") == "state":
                out.update(state=(event.get("payload") or {}).get("to"),
                           since=event.get("ts"))
                break
    except OSError:
        pass
    return out


TAIL_EVENTS = 200          # what a fresh reader starts with: the newest events, not the file
READ_BYTES = 1024 * 1024   # a read never takes more than this at once


def follow(home, *, after=None, session=None, tail=TAIL_EVENTS, forever=True, newest=None,
           sleep=time.sleep, poll=0.25):
    """The primary stream's events, then each new one as it is appended,
    following the runner to a new stream when it restarts (the old file
    drained first, so its last events are never cut off).

    Where it starts: a reader that names the `session` it has and the last
    `after` it saw resumes right after that event. One that names a session
    that is no longer the primary (the runner restarted meanwhile) is told
    ("session", <stem>) and starts the new stream at its tail, like a fresh
    reader: its last `tail` events (all of them for tail None or 0).
    `after` alone applies to the current primary stream.

    Yields ("start", <stem>) first for the file it begins with (the label
    of what follows, nothing new to the reader), ("event", event),
    ("session", <stem>) whenever the file changes (the runner's first file
    appearing, a restart, a resumed session that is gone), and ("idle",
    None) once per poll that found nothing; with forever=False it stops at
    the first idle poll (caught up). Every event is preceded by the
    "start" or "session" naming its file, so a reader labels from these
    yields alone. Reads at most READ_BYTES at a time, from a byte
    offset, never the whole file; a torn last line waits for its end.
    `newest(home) -> Path | None` defaults to primary_stream."""
    newest = newest or primary_stream
    path, offset, pending = newest(home), 0, b""
    if path is not None:
        if session is not None and path.stem != session:
            yield "session", path.stem
            after = None
        else:
            yield "start", path.stem
        try:
            offset = _offset(path, tail=None if after is not None else tail, after=after)
        except OSError:
            offset = 0
    while True:
        found, more = False, False
        if path is not None:
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    data = fh.read(READ_BYTES)
                    more = len(data) == READ_BYTES
            except OSError:
                data = b""
            offset += len(data)
            pending += data
            *lines, pending = pending.split(b"\n")
            for event in _events(line.decode("utf-8", "replace") for line in lines):
                if after is None or int(event.get("seq") or 0) > after:
                    found = True
                    yield "event", event
        if more:
            continue                          # the rest of a long file, a bounded read at a time
        current = newest(home)
        if current is not None and current != path:
            found = True                      # the runner's first file, or a restart
            yield "session", current.stem
            path, offset, pending, after = current, 0, b"", None
            continue
        if not found:
            yield "idle", None
            if not forever:
                return
        sleep(poll)
