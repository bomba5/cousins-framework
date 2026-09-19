"""Meeting routes over cousin_lib.meetings (docs/meetings.md,
docs/reference/console-api.md "Meetings"). The console is a view and an
entry point: the user opens, posts, skips and closes here, cousins
speak through cousin-meeting, and both write the same store. The
speaker of every user action is the signed-in console user."""
from __future__ import annotations

from cousin_lib import meetings
from cousin_lib.console import router
from cousin_lib.console.app import HttpError


def _mid(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HttpError(404, "meeting #%s not found" % raw)


def _user(req):
    return req.user or "user"


def _call(req, fn, *args, **kw):
    try:
        result = fn(*args, root=req.server.root, **kw)
    except meetings.MeetingNotFound as err:
        raise HttpError(404, str(err))
    except meetings.MeetingError as err:
        raise HttpError(400, str(err))
    return result


def register():
    @router.route("GET", "/api/meetings")
    def list_(req):
        return 200, {"meetings": _call(req, meetings.list_meetings,
                                       state=req.query.get("state") or None)}

    @router.route("POST", "/api/meetings")
    def open_(req):
        body = req.body
        participants = body.get("participants")
        if not isinstance(participants, list):
            raise HttpError(400, "participants must be a list of slugs")
        m = _call(req, meetings.open_meeting, body.get("topic") or "",
                  [str(p) for p in participants], created_by=_user(req),
                  facilitator=str(body.get("facilitator") or ""),
                  timeout_s=body.get("timeout_s",
                                     meetings.DEFAULT_TIMEOUT_S))
        req.server.emit("meeting-change", {"id": m["id"], "op": "open"})
        return 200, {"ok": True, "meeting": m}

    @router.route("GET", "/api/meetings/{meeting_id}")
    def show(req, meeting_id):
        return 200, {"meeting": _call(req, meetings.show, _mid(meeting_id))}

    @router.route("POST", "/api/meetings/{meeting_id}/post")
    def post(req, meeting_id):
        text = req.body.get("text")
        if not isinstance(text, str):
            raise HttpError(400, "text must be a string")
        m = _call(req, meetings.post, _mid(meeting_id), _user(req), text)
        req.server.emit("meeting-change", {"id": m["id"], "op": "post"})
        return 200, {"ok": True, "meeting": m}

    @router.route("POST", "/api/meetings/{meeting_id}/skip")
    def skip(req, meeting_id):
        m = _call(req, meetings.skip, _mid(meeting_id), _user(req))
        req.server.emit("meeting-change", {"id": m["id"], "op": "skip"})
        return 200, {"ok": True, "meeting": m}

    @router.route("POST", "/api/meetings/{meeting_id}/close")
    def close(req, meeting_id):
        m = _call(req, meetings.close, _mid(meeting_id), _user(req))
        req.server.emit("meeting-change", {"id": m["id"], "op": "close"})
        return 200, {"ok": True, "meeting": m}


register()
