"""The live turn: which threads the model is answering right now.

`reply` routes by it (spec, "Features as in-process tools"): one live
thread means a reply with no destination goes there; two (an operator
message folded into a running turn, finding 1) means the destination
must be named. The runner owns the writes; tool handlers only read.
"""
import threading


class Turn:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._active = False

    def begin(self, row):
        with self._lock:
            self._rows = [dict(row)]
            self._active = True

    def add(self, row):
        with self._lock:
            if not self._active:
                raise RuntimeError("no turn is running")
            self._rows.append(dict(row))

    def end(self):
        with self._lock:
            self._rows = []
            self._active = False

    def snapshot(self):
        """(active, threads) read under one lock: two property reads can
        straddle a begin or an end and disagree."""
        with self._lock:
            out = []
            for r in self._rows:
                if r["thread_id"] not in out:
                    out.append(r["thread_id"])
            return self._active, tuple(out)

    @property
    def active(self):
        with self._lock:
            return self._active

    @property
    def threads(self):
        with self._lock:
            out = []
            for r in self._rows:
                if r["thread_id"] not in out:
                    out.append(r["thread_id"])
            return tuple(out)

    @property
    def inbox_ids(self):
        with self._lock:
            return tuple(r["id"] for r in self._rows)

    @property
    def origin(self):
        with self._lock:
            return self._rows[0]["thread_id"] if self._rows else None

    @property
    def senders(self):
        with self._lock:
            out = {}
            for r in self._rows:
                out.setdefault(r["thread_id"], r.get("sender") or "")
            return out
