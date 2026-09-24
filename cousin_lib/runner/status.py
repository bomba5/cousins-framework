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
    """The file's complete lines, last first, read CHUNK_BYTES at a time."""
    with open(path, "rb") as fh:
        pos = fh.seek(0, 2)
        rest = b""
        while pos > 0:
            step = min(CHUNK_BYTES, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + rest
            lines = data.split(b"\n")
            rest = lines[0]                 # may continue in the chunk before
            for line in reversed(lines[1:]):
                if line:
                    yield line.decode("utf-8", "replace")
        if rest:
            yield rest.decode("utf-8", "replace")


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
        for event in _events(_backwards(path)):
            if event.get("kind") == "state":
                out.update(state=(event.get("payload") or {}).get("to"),
                           since=event.get("ts"))
                break
    except OSError:
        pass
    return out
