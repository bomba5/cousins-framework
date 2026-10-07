"""`GET /api/events`: the live stream every dashboard tab holds
(docs/reference/console-api.md, "`GET /api/events`: the live stream").

Three parts, none of them a store:

- the broker: `subscribe()`/`emit()`; a command handler that starts or
  stops a cousin, runs a flip or mutates the tracker calls `emit` at the
  moment it acts, and every open stream receives the frame;
- the poller: one thread that re-reads the stores other components own
  (`jobs.db` and the request store every 2 s, the fleet, the loops rows,
  the fire state and the tracker every 15 s or 2 s as the contract says)
  and emits only the differences; a dropped frame costs a reconnect and
  a fresh snapshot, never data;
- the stream: a generator the server iterates, snapshot first, then
  frames as they arrive, a `: ping` comment when nothing has for a
  while, and an unsubscribe in its `finally` so a disconnect (the server
  stops iterating and calls `close()`) releases the queue.

The rows the snapshot and the refresh events carry are the same rows the
GET views return; the server plugs those enrichers in with `configure`
(the defaults here are the bare registry and an empty loops list, so the
stream works, minus enrichment, before the fleet routes are wired).
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time

from cousin_lib import jobs, loops, meetings, tracker
from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router

PING = b": ping\n\n"
_FAST = 2.0    # jobs, requests, tracker
_SLOW = 15.0   # fleet, loops rows, fire state


def frame(kind, data):
    """One `data:` frame carrying {"kind", "data"}."""
    return ("data: %s\n\n" % json.dumps({"kind": kind, "data": data})).encode()


def event_frame(event, data):
    """One named event frame (the pane stream uses these)."""
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode()


class Stream:
    """A streaming response a route handler returns as the body of a
    200: the server sends `content_type` and `headers`, then writes and
    flushes each chunk `iter(stream)` yields until the client goes away
    (BrokenPipeError / ConnectionResetError / OSError), and calls
    `close()` in every case so the generator's finally runs."""

    content_type = "text/event-stream"

    def __init__(self, chunks, headers=None):
        self._chunks = chunks
        self.headers = list(headers) if headers is not None else [
            ("Cache-Control", "no-cache"),
            ("X-Accel-Buffering", "no"),
            ("Connection", "keep-alive"),
        ]

    def __iter__(self):
        return self._chunks

    def close(self):
        close = getattr(self._chunks, "close", None)
        if close is not None:
            close()


# ---- broker ------------------------------------------------------------

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()


def subscribe(maxsize=200):
    q = queue.Queue(maxsize=maxsize)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q):
    with _lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def subscriber_count():
    with _lock:
        return len(_subscribers)


def emit(kind, data):
    """Fan one event out to every open stream. A subscriber whose queue
    is full is too slow or already gone: it is dropped, never waited
    on; its client reconnects and gets a fresh snapshot."""
    payload = json.dumps({"kind": kind, "data": data})
    with _lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


# ---- sources -----------------------------------------------------------

def _registry_rows(root):
    """The bare fleet projection: enough for a snapshot before the fleet
    routes plug in their enriched rows. Model and effort are the same
    effective values the fleet rows carry (the cousin's [runtime], else
    the install's [agent] defaults), read through the fleet module's
    helpers so the two projections cannot disagree."""
    from cousin_lib.console.routes_fleet import (agent_defaults,
                                                 effective_runtime)
    defaults = agent_defaults(root)
    rows = []
    for c in FrameworkConfig(root).list_cousins():
        rows.append({
            "slug": c.slug, "name": c.name, "type": c.type, "home": str(c.home),
            "tmuxSession": c.tmux_session, "operator": c.operator_name,
            "memoryScope": c.memory_scope, "heartbeat": c.heartbeat_seconds,
            "flipAt": c.flip_at, **effective_runtime(c, defaults),
        })
    return rows


def _default_sources(root):
    return {
        "cousins": lambda: _registry_rows(root),
        "loops": lambda: [],
        "daemon": loops.daemon_status,
        "jobs": lambda: (jobs.reap_lost(), jobs.list_jobs(limit=200))[1],
        "last_fires": lambda: loops._load_state().get("last_fires") or {},
        "requests": lambda: loops.list_requests(limit=200),
        "tracker": lambda: tracker.list_items(root=root),
        "meetings": lambda: meetings.list_meetings(root=root),
    }


_configured: dict = {}


def configure(**sources):
    """Plug the enrichers the GET views use (`cousins=`, `loops=`, or
    any other source name) into every poller and snapshot built after
    this call."""
    _configured.update(sources)


def _sources(root, override=None):
    merged = _default_sources(root)
    merged.update(_configured)
    merged.update(override or {})
    return merged


def _call(sources, name, default):
    try:
        return sources[name]()
    except Exception as err:  # noqa: BLE001 - a store's failure is logged, never fatal
        print("[console] events: %s source failed: %s" % (name, err),
              file=sys.stderr, flush=True)
        return default


def snapshot(*, root=None, sources=None):
    root = FrameworkConfig.resolve(root).root
    src = _sources(root, sources)
    return {
        "cousins": _call(src, "cousins", []),
        "loops": _call(src, "loops", []),
        "jobs": _call(src, "jobs", []),
        "daemon": _call(src, "daemon",
                        {"ok": False, "message": "loops daemon status unknown"}),
    }


# ---- poller ------------------------------------------------------------

class Poller:
    """Diffs the stores on a schedule and emits the differences. The
    first poll is a baseline: it emits the refresh events (they carry
    whole rows and are idempotent) and remembers everything else without
    announcing it, because the connecting tab already has the snapshot."""

    def __init__(self, *, root=None, sources=None, fast=_FAST, slow=_SLOW,
                 clock=time.time, emit=emit):
        self.root = FrameworkConfig.resolve(root).root
        self.sources = _sources(self.root, sources)
        self.fast, self.slow = fast, slow
        self.clock, self.emit = clock, emit
        self._next_fast = self._next_slow = None
        self._jobs = None
        self._tracker = None
        self._meetings = None
        self._flips = None
        self._fires = None
        self._thread = None
        self._stop = threading.Event()

    # -- one pass

    def poll(self, now=None):
        now = self.clock() if now is None else now
        events = []
        if self._next_fast is None or now >= self._next_fast:
            self._next_fast = now + self.fast
            self._diff_jobs(events)
            self._diff_flips(events)
            self._diff_tracker(events)
            self._diff_meetings(events)
        if self._next_slow is None or now >= self._next_slow:
            self._next_slow = now + self.slow
            events.append(("cousins-refresh", _call(self.sources, "cousins", [])))
            events.append(("loops-refresh", _call(self.sources, "loops", [])))
            self._diff_fires(events)
        for kind, data in events:
            self.emit(kind, data)
        return events

    def _diff_jobs(self, events):
        rows = {r["id"]: r for r in _call(self.sources, "jobs", [])}
        if self._jobs is not None:
            for jid, row in rows.items():
                if jid not in self._jobs:
                    events.append(("job-add", row))
                elif row != self._jobs[jid]:
                    events.append(("job-update", row))
            for jid in self._jobs:
                if jid not in rows:
                    events.append(("job-delete", {"id": jid}))
        self._jobs = rows

    def _diff_flips(self, events):
        rows = {r["id"]: r for r in _call(self.sources, "requests", [])
                if r.get("kind") == "flip"}
        if self._flips is not None:
            for rid, row in rows.items():
                before = self._flips.get(rid)
                status = row.get("status")
                if before is not None and before.get("status") == status:
                    continue
                if status == "pending" and before is None:
                    try:
                        payload = json.loads(row.get("payload") or "{}")
                    except ValueError:
                        payload = {}
                    events.append(("cousin-flip", {
                        "slug": row["cousin"], "phase": "scheduled",
                        "fire_at": payload.get("fire_at"),
                        "request_id": rid}))
                elif status == "done":
                    events.append(("cousin-flip", {
                        "slug": row["cousin"], "phase": "complete",
                        "ok": True, "request_id": rid}))
                elif status in ("failed", "expired"):
                    events.append(("cousin-flip", {
                        "slug": row["cousin"], "phase": "failed",
                        "ok": False, "error": row.get("reason") or status,
                        "request_id": rid}))
                elif status == "cancelled":
                    events.append(("cousin-flip", {
                        "slug": row["cousin"], "phase": "cancelled",
                        "request_id": rid}))
        self._flips = rows

    def _diff_tracker(self, events):
        rows = {r["id"]: r for r in _call(self.sources, "tracker", [])}
        if self._tracker is not None:
            for iid, row in rows.items():
                if iid not in self._tracker:
                    events.append(("tracker-change", {"id": iid, "op": "add"}))
                elif row != self._tracker[iid]:
                    events.append(("tracker-change",
                                   {"id": iid, "op": "update"}))
            for iid in self._tracker:
                if iid not in rows:
                    events.append(("tracker-change",
                                   {"id": iid, "op": "delete"}))
        self._tracker = rows

    def _diff_meetings(self, events):
        """Cousins speak through the CLI, outside the console: a change
        in a meeting's row or entry count is the only signal."""
        rows = {m["id"]: (m["updated_at"], m.get("entries"))
                for m in _call(self.sources, "meetings", [])}
        if self._meetings is not None:
            for mid, sig in rows.items():
                if self._meetings.get(mid) != sig:
                    events.append(("meeting-change",
                                   {"id": mid, "op": "update"}))
        self._meetings = rows

    def _diff_fires(self, events):
        fires = dict(_call(self.sources, "last_fires", {}))
        if self._fires is not None:
            for key, ts in fires.items():
                if ts and ts > (self._fires.get(key) or 0):
                    slug, _, name = key.partition("|")
                    events.append(("loop-fire",
                                   {"cousin": slug, "loop": name, "ts": ts}))
        self._fires = fires

    # -- the thread

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as err:  # noqa: BLE001 - the thread outlives any one failure
                print("[console] events poll failed: %s" % err,
                      file=sys.stderr, flush=True)
            self._stop.wait(min(self.fast, self.slow))

    def start(self):
        if self.running:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="console-events-poller")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


_poller: Poller | None = None


def start_poller(**kw):
    """The process-wide poller; the server starts it once at boot."""
    global _poller
    if _poller is None:
        _poller = Poller(**kw)
    _poller.start()
    return _poller


def stop_poller():
    global _poller
    if _poller is not None:
        _poller.stop()
        _poller = None


def reset():
    """Tests: forget subscribers, configured sources and the poller."""
    stop_poller()
    with _lock:
        del _subscribers[:]
    _configured.clear()


# ---- the stream --------------------------------------------------------

def _generate(q, first, ping_after):
    try:
        yield first
        while True:
            try:
                payload = q.get(timeout=ping_after)
            except queue.Empty:
                yield PING
                continue
            yield ("data: %s\n\n" % payload).encode()
    finally:
        unsubscribe(q)


def events_stream(*, ping_after=25.0, root=None, sources=None):
    """The response body for `GET /api/events`: subscribe BEFORE taking
    the snapshot so nothing between the two is lost, then snapshot,
    then frames, then pings."""
    q = subscribe()
    try:
        first = frame("snapshot", snapshot(root=root, sources=sources))
    except Exception:
        unsubscribe(q)
        raise
    return Stream(_generate(q, first, ping_after))


def register():
    @router.route("GET", "/api/events")
    def events(req):
        return 200, events_stream(root=getattr(req, "root", None))


register()
