"""The reasoning pane of a runner cousin (spec, "Views";
docs/reference/console-api.md, "The runner stream"): its event stream
live, an interrupt, and a "say" box that enqueues. Three routes, runner
cousins only (a tmux cousin has the tmux pane, `/api/pane/*`: 409 here):

- `GET /api/cousins/{slug}/stream`: SSE over the runner's primary stream
  (runner.status.primary_stream: the newest `data/stream/<session>.jsonl`
  headed by a `runner` event). A fresh connect starts at the newest
  TAIL_EVENTS events. Each event is one frame, `event: runner-event`,
  `id: <session>:<seq>`, data the event as written (`{seq, ts, kind,
  payload}`). A reconnect's `Last-Event-ID` (or `after`) resumes right after
  that event; one whose session is no longer the primary gets `event:
  session` first and the new stream from its newest TAIL_EVENTS. A runner
  that restarts while connected is followed after its old file is drained.
  `: ping` after 15 s of silence. Reads come from a byte offset, at most
  runner.status.READ_BYTES at a time.
- `POST /api/cousins/{slug}/interrupt`: an `interrupt` inbox row
  (runner/base.INTERRUPT), waited on up to 5 s: `delivered` means the live
  turn was interrupted; `failed` that no turn was running, or that the
  agent refused the interrupt (the error is in the row's detail); `queued`
  that the runner did not answer in time. 409 when no runner holds the
  cousin's lock: an interrupt left for a later start would interrupt
  nothing.
- `POST /api/cousins/{slug}/say {text}`: a chat item on the operator's
  thread (`operator:<name>`), not stored in chat.db: the pane's input, as
  typing into a tmux pane is. A login code is diverted first, as on every
  operator send path (`diverted`, nothing delivered); anything else answers
  `queued` (the runner folds it into a live turn, or takes it next).
"""
from __future__ import annotations

import json
import time

from cousin_lib import delivery
from cousin_lib.console import router, sse
from cousin_lib.console.proxy import RouteError, find_cousin, guarded, serves_locally

INTERRUPT_WAIT_S = 5.0


def _runner_cousin(req, slug):
    cousin = find_cousin(req, slug)
    if not serves_locally(cousin):
        raise RouteError(409, {"ok": False,
                               "error": "not a runner cousin: its view is the tmux pane"})
    return cousin


from cousin_lib.runner.status import TAIL_EVENTS  # noqa: E402 - one number, one place


def _resume(req):
    """(session, after) the client has: `Last-Event-ID` on a reconnect
    (`<session>:<seq>`), else the `after` query parameter (`<seq>`, in the
    current stream, or `<session>:<seq>`); (None, None) for a fresh connect."""
    raw = req.query.get("after")
    headers = getattr(req, "headers", None)
    if raw in (None, "") and headers is not None:
        raw = headers.get("Last-Event-ID")
    if raw in (None, ""):
        return None, None
    session, _, seq = str(raw).rpartition(":")
    try:
        return (session or None), max(0, int(seq))
    except ValueError:
        raise RouteError(400, {"ok": False,
                               "error": "after must be <seq> or <session>:<seq>"})


def runner_frame(event, session):
    """One event of the stream as an SSE frame; its id names the session
    too, so a reconnect after a runner restart is recognised."""
    return ("id: %s:%d\nevent: runner-event\ndata: %s\n\n"
            % (session, int(event.get("seq") or 0), json.dumps(event))).encode()


def stream_events(home, after=None, session=None, *, tail=TAIL_EVENTS, newest=None,
                  sleep=time.sleep, clock=time.monotonic, poll=0.25, ping_after=15.0):
    """Generator of SSE bytes over runner.status.follow: a `runner-event`
    frame per event, a `session` frame when the runner restarted, `: ping`
    after `ping_after` seconds without either. A fresh connect starts at
    the last `tail` events; a reconnect resumes after the event it names."""
    from cousin_lib.runner import status
    last_out = clock()
    current = None               # the label comes from follow alone: "start" or "session"
    for what, value in status.follow(home, after=after, session=session, tail=tail,
                                     newest=newest, sleep=sleep, poll=poll):
        now = clock()
        if what == "start":
            current = value
        elif what == "event":
            last_out = now
            yield runner_frame(value, current)
        elif what == "session":
            last_out, current = now, value
            yield sse.event_frame("session", {"session": value})
        elif now - last_out >= ping_after:
            last_out = now
            yield sse.PING


def register():
    @router.route("GET", "/api/cousins/{slug}/stream")
    @guarded
    def stream(req, slug):
        cousin = _runner_cousin(req, slug)
        session, after = _resume(req)
        return 200, sse.Stream(stream_events(cousin.home, after, session))

    @router.route("POST", "/api/cousins/{slug}/interrupt")
    @guarded
    def interrupt(req, slug):
        from cousin_lib.runner.base import INTERRUPT
        from cousin_lib.runner.main import is_running
        cousin = _runner_cousin(req, slug)
        if not is_running(cousin.home):
            raise RouteError(409, {"ok": False, "error": "no runner is running"})
        who = getattr(req, "user", None) or "the console"
        item = delivery.Item(thread_id=delivery.thread_id("system"), source=INTERRUPT,
                             body="interrupt asked by %s from the console" % who, sender=who)
        outcome = delivery.deliver(cousin.home, item, wait=True, timeout=INTERRUPT_WAIT_S)
        return 200, {"ok": outcome == delivery.DELIVERED, "outcome": outcome}

    @router.route("POST", "/api/cousins/{slug}/say")
    @guarded
    def say(req, slug):
        cousin = _runner_cousin(req, slug)
        text = req.body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RouteError(400, {"ok": False, "error": "text is required"})
        operator = cousin.operator_name
        if not operator:
            raise RouteError(409, {"ok": False, "error": "the cousin has no operator configured"})
        # every operator send path diverts a login code first
        from cousin_lib.server.inbound import divert_login_code
        if divert_login_code(cousin, operator, text) is not None:
            return 200, {"ok": True, "outcome": "diverted"}
        item = delivery.Item(thread_id=delivery.thread_id("operator", operator), source="chat",
                             body=text, sender=operator)
        outcome = delivery.deliver(cousin.home, item, wait=False)
        return 200, {"ok": outcome != delivery.FAILED, "outcome": outcome}


register()
