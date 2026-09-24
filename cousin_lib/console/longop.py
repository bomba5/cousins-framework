"""Long operations on one cousin: the flip thread's pattern
(routes_fleet flip_start), generalized for the packages that need it (a
kind switch or migration apply, an account login flow).

One operation per cousin at a time, and never beside a running flip or
clean stop: the table shares routes_fleet's `flip_lock`, and each side
checks the other. The work runs on a daemon thread; it reports stages as
they happen, each one a `cousin-op` event on /api/events, and its state
stays readable at GET /api/cousins/<slug>/op until the next one starts.

The API a route module uses:

    from cousin_lib.console import longop

    def work(op):                       # runs on the op's thread
        op.stage("close", "running")
        ...
        op.stage("close", "done", "3 rows imported")   # detail: short text
        return {"ok": True, ...}        # the result; ok False fails the op

    @router.route("POST", "/api/cousins/{slug}/migrate/apply")
    def apply(req, slug):
        cousin_home(req.server, slug)
        return longop.start_response(req.server, slug, "migrate", work,
                                     params={"to": "sdk"})   # 202, or 409 busy

A raise inside `work` fails the op, whatever it raises (a SystemExit
from an argparse main included): an OpError with its own words, anything
else with "failed: <ExceptionType>, see the console log" while the full
text and traceback go to the console's stderr only (an exception's text
can carry a secret). Never put a secret in an OpError, a stage detail,
the params or the result: all of them are served and broadcast as they
are.

The event: `cousin-op` {slug, id, kind, phase: started|stage|done|failed,
stage?, error?}; the browser gets it as a window `fw-cousin-op` event
(app.jsx), and ui.jsx's useLongOp(slug) and <LongOpStatus> read it."""
from __future__ import annotations

import sys
import threading
import time
import traceback
import uuid

from cousin_lib.console import router

EVENT = "cousin-op"
STAGE_STATES = ("running", "done", "failed", "skipped")
DETAIL_MAX = 500


class Busy(Exception):
    """Another operation, a flip or a clean stop runs on this cousin."""


class OpError(Exception):
    """A failure `work` words for the operator: its message is served and
    broadcast as it is, so it must never hold a secret."""


def _lock(server):
    return server.state.setdefault("flip_lock", threading.Lock())


def _ops(server):
    return server.state.setdefault("ops", {})


def running(server, slug):
    """The kind of what runs on this cousin now (an op's kind, "flip" or
    "clean stop"), or None. Call it with the lock held to act on it."""
    op = _ops(server).get(slug)
    if op and op["status"] == "running":
        return op["kind"]
    flip = server.state.get("flips", {}).get(slug)
    if flip and flip.get("status") == "running":
        return "clean stop" if flip.get("kind") == "stop" else "flip"
    return None


def op_running(server, slug):
    """The running op's kind on this cousin, or None: what routes_fleet's
    flip and clean stop check before they start."""
    op = _ops(server).get(slug)
    return op["kind"] if op and op["status"] == "running" else None


def _public(entry):
    out = {k: entry[k] for k in ("id", "slug", "kind", "status", "started_at",
                                 "finished_at", "params", "result", "error")}
    out["stages"] = [dict(s) for s in entry["stages"]]
    return out


class Op:
    """What `work` is handed: stage() reports progress."""

    def __init__(self, server, entry):
        self._server = server
        self._entry = entry
        self.id = entry["id"]
        self.slug = entry["slug"]
        self.kind = entry["kind"]
        self.params = entry["params"]

    def stage(self, name, status="running", detail=None):
        if status not in STAGE_STATES:
            raise ValueError("stage status must be one of %s" % ", ".join(STAGE_STATES))
        detail = None if detail is None else str(detail)[:DETAIL_MAX]
        row = {"name": str(name), "status": status, "detail": detail, "at": time.time()}
        stages = self._entry["stages"]
        # a stage reported again moves on in place: running -> done
        for i, old in enumerate(stages):
            if old["name"] == row["name"]:
                stages[i] = row
                break
        else:
            stages.append(row)
        self._emit("stage", stage=dict(row))

    def _emit(self, phase, **extra):
        self._server.emit(EVENT, {"slug": self.slug, "id": self.id, "kind": self.kind,
                                  "phase": phase, **extra})


def start(server, slug, kind, work, *, params=None):
    """Run `work(op)` on a thread as this cousin's operation; the op's
    public state. Busy when anything already runs on the cousin."""
    with _lock(server):
        busy = running(server, slug)
        if busy:
            raise Busy("a %s is already running on %s" % (busy, slug))
        entry = {"id": uuid.uuid4().hex[:12], "slug": slug, "kind": str(kind),
                 "status": "running", "started_at": time.time(), "finished_at": None,
                 "params": dict(params or {}), "stages": [], "result": None, "error": None}
        _ops(server)[slug] = entry
    op = Op(server, entry)

    def run():
        result, error = None, "failed: the operation ended without a result"
        try:
            result = work(op)
            error = None
            if isinstance(result, dict) and result.get("ok") is False:
                error = str(result.get("error") or "the operation failed")
        except OpError as err:
            result, error = None, str(err)
        except BaseException as err:  # noqa: BLE001 - SystemExit too: the op must end
            result, error = None, "failed: %s, see the console log" % type(err).__name__
            print("cousin-console: op %s %s on %s failed:\n%s" % (
                entry["kind"], entry["id"], slug, traceback.format_exc()),
                file=sys.stderr, flush=True)
        finally:
            _finish(result, error)

    def _finish(result, error):
        if error:
            for s in entry["stages"]:
                if s["status"] == "running":
                    s["status"], s["detail"] = "failed", s["detail"] or error[:DETAIL_MAX]
        entry["result"] = result if isinstance(result, dict) else (
            None if result is None else {"value": result})
        entry["error"] = error
        entry["finished_at"] = time.time()
        entry["status"] = "failed" if error else "done"
        op._emit(entry["status"], **({"error": error} if error else {}))

    op._emit("started")
    started = _public(entry)        # before the thread can move it on
    threading.Thread(target=run, daemon=True,
                     name="console-op-%s-%s" % (kind, slug)).start()
    return started


def status(server, slug):
    """The cousin's current or last operation's public state, or None."""
    entry = _ops(server).get(slug)
    return None if entry is None else _public(entry)


def forget(server, slug):
    """Drop a finished op's state (a dismissed cousin); a running one stays."""
    with _lock(server):
        entry = _ops(server).get(slug)
        if entry and entry["status"] != "running":
            del _ops(server)[slug]


def start_response(server, slug, kind, work, *, params=None):
    """start() as a route answer: (202, {"ok": true, "op"}), or an
    HttpError 409 when something already runs on the cousin."""
    from cousin_lib.console.app import HttpError
    try:
        return 202, {"ok": True, "op": start(server, slug, kind, work, params=params)}
    except Busy as err:
        raise HttpError(409, str(err), busy=True)


def register():
    from cousin_lib.console._common import cousin_home

    @router.route("GET", "/api/cousins/{slug}/op")
    def get_op(req, slug):
        cousin_home(req.server, slug)
        return 200, {"ok": True, "op": status(req.server, slug)}


register()
