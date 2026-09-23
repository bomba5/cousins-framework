"""The event stream: every SDK message and state change, appended as
one JSON line, readable live by any number of readers. The reasoning
pane is a projection of this file."""
import json
import threading
import time
from pathlib import Path


class EventStream:
    def __init__(self, home, session_id):
        self.path = Path(home) / "data" / "stream" / ("%s.jsonl" % session_id)
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
            with open(self.path, "a+b") as f:
                if f.seek(0, 2) > 0:  # file is non-empty
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
            return self._seq

    def tail(self, after=None):
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.endswith("\n"):
                    return          # a writer died mid-line; the next append completes the file
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if after is not None and event.get("seq", 0) <= after:
                    continue
                yield event
