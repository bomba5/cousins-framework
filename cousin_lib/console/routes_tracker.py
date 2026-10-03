"""Tracker routes over cousin_lib.tracker (docs/jobs-and-loops.md for the
shapes, docs/reference/console-api.md for the routes): list open-first, create,
update, delete, with ItemNotFound as 404 and any other TrackerError as
400, and a tracker-change event per mutation. A change made here is
recorded in the item's history under the logged-in user, else
'operator'; showing one item returns its history with it."""
from __future__ import annotations

from cousin_lib import tracker
from cousin_lib.console import router
from cousin_lib.console.app import HttpError

_FIELDS = ("title", "domain", "state", "tags", "owner", "notes")
_UPDATE_ONLY = ("add_note",)


def _item_id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HttpError(404, "tracker item #%s not found" % raw)


def _who(req):
    return req.user or "operator"


def _fields(body, *, require_title):
    out = {}
    keys = _FIELDS if require_title else _FIELDS + _UPDATE_ONLY
    for key in keys:
        if key in body and body[key] is not None:
            value = body[key]
            if key == "tags":
                if not isinstance(value, list):
                    raise HttpError(400, "tags must be a list")
                value = [str(v) for v in value]
            elif not isinstance(value, str):
                raise HttpError(400, "%s must be a string" % key)
            out[key] = value
    if require_title and not (out.get("title") or "").strip():
        raise HttpError(400, "title required")
    return out


def register():
    @router.route("GET", "/api/tracker")
    def list_items(req):
        q = req.query
        try:
            items = tracker.list_items(domain=q.get("domain") or None,
                                       state=q.get("state") or None,
                                       tag=q.get("tag") or None,
                                       owner=q.get("owner") or None,
                                       root=req.server.root)
        except tracker.TrackerError as err:
            raise HttpError(400, str(err))
        return 200, {"items": items}

    @router.route("POST", "/api/tracker")
    def create(req):
        fields = _fields(req.body, require_title=True)
        try:
            item = tracker.add(fields.pop("title"), root=req.server.root,
                               who=_who(req), **fields)
        except tracker.TrackerError as err:
            raise HttpError(400, str(err))
        req.server.emit("tracker-change", {"id": item["id"], "op": "add"})
        return 200, {"ok": True, "item": item}

    @router.route("GET", "/api/tracker/{item_id}")
    def show(req, item_id):
        item = tracker.show(_item_id(item_id), root=req.server.root)
        if item is None:
            raise HttpError(404, "tracker item #%s not found" % item_id)
        return 200, {"item": item,
                     "history": tracker.history(item["id"],
                                                root=req.server.root)}

    @router.route("POST", "/api/tracker/{item_id}")
    def update(req, item_id):
        ident = _item_id(item_id)
        fields = _fields(req.body, require_title=False)
        try:
            item = tracker.update(ident, root=req.server.root, who=_who(req),
                                  **fields)
        except tracker.ItemNotFound as err:
            raise HttpError(404, str(err))
        except tracker.TrackerError as err:
            raise HttpError(400, str(err))
        req.server.emit("tracker-change", {"id": ident, "op": "update"})
        return 200, {"ok": True, "item": item}

    @router.route("DELETE", "/api/tracker/{item_id}")
    def delete(req, item_id):
        ident = _item_id(item_id)
        if not tracker.delete(ident, who=_who(req), root=req.server.root):
            raise HttpError(404, "tracker item #%d not found" % ident)
        req.server.emit("tracker-change", {"id": ident, "op": "delete"})
        return 200, {"ok": True, "deleted": ident}


register()
