"""The event stream: every SDK message and state change, appended as
one JSON line, readable live by any number of readers. The reasoning
pane is a projection of this file."""
import json
import threading
import time
from pathlib import Path

from cousin_lib import jsonl


def path_for(home, session_id):
    return Path(home) / "data" / "stream" / ("%s.jsonl" % session_id)


def results_naming(path, ids):
    """{inbox id: the `result` event that names it} for `ids`, read from the
    stream file at `path` (a dead runner's: no writer). Complete lines only."""
    want, found = set(ids), {}
    try:
        f = open(path, "rb")
    except OSError:
        return found
    with f:
        for raw in f:
            if b'"result"' not in raw or not raw.endswith(b"\n"):
                continue
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if event.get("kind") != "result":
                continue
            for i in (event.get("payload") or {}).get("inbox_ids") or ():
                if i in want:
                    found[i] = event
    return found


class EventStream:
    def __init__(self, home, session_id):
        self.path = path_for(home, session_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        for event in self.tail():
            self._seq = event["seq"]

    def append(self, kind, payload):
        with self._lock:
            self._seq += 1
            line = json.dumps({"seq": self._seq, "ts": time.time(),
                               "kind": kind, "payload": payload},
                              ensure_ascii=False)
            # one write, a torn tail closed first (jsonl.append_line): an
            # event over a buffer's size (a tool's output) no longer tears
            # mid-line on a kill
            jsonl.append_line(self.path, line)
            return self._seq

    def tail(self, after=None):
        if not self.path.exists():
            return
        # bytes, decoded one complete line at a time: a writer that
        # died inside a multi-byte character leaves bytes a text-mode read
        # would raise on before the partial line could be skipped
        with open(self.path, "rb") as f:
            for raw in f:
                if not raw.endswith(b"\n"):
                    return          # a writer died mid-line; the next append completes the file
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if after is not None and event.get("seq", 0) <= after:
                    continue
                yield event
