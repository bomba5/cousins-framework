"""Per-user console preferences (docs/reference/console-api.md,
"Preferences"). Today one: the sidebar groups, {groups, assignments}.
They were a browser preference in local storage, so another browser or
the phone's home-screen app never saw them; here they live on the
server, one file per console user under data/console-prefs/, and every
browser that user logs in from reads the same layout.

Open mode (no users file) stores under one shared name."""
from __future__ import annotations

import json
import os
import re
import tempfile

from cousin_lib.console import router
from cousin_lib.console.app import HttpError

OPEN_USER = "_open"
MAX_BYTES = 64 * 1024
_SAFE_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def _path(server, user):
    name = user or OPEN_USER
    if not _SAFE_USER.match(name) or name.startswith("."):
        raise HttpError(400, "user name cannot name a preferences file")
    return server.root / "data" / "console-prefs" / ("%s.json" % name)


def _read(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_sidebar(value):
    """{groups: [{id, name, collapsed}], assignments: {slug: group id}};
    at least one group, unique ids. Returns the normalised value."""
    if not isinstance(value, dict):
        raise HttpError(400, "sidebar must be an object")
    groups, assignments = value.get("groups"), value.get("assignments", {})
    if not isinstance(groups, list) or not groups:
        raise HttpError(400, "sidebar.groups must be a non-empty list")
    out, seen = [], set()
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str) \
                or not g["id"] or not isinstance(g.get("name"), str):
            raise HttpError(400, "each group needs a string id and name")
        if g["id"] in seen:
            raise HttpError(400, "duplicate group id %r" % g["id"])
        seen.add(g["id"])
        out.append({"id": g["id"], "name": g["name"],
                    "collapsed": bool(g.get("collapsed", False))})
    if not isinstance(assignments, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in assignments.items()):
        raise HttpError(400, "sidebar.assignments must map slug to group id")
    return {"groups": out, "assignments": dict(assignments)}


def register():
    @router.route("GET", "/api/prefs/sidebar")
    def get_sidebar(req):
        prefs = _read(_path(req.server, req.user))
        return 200, {"sidebar": prefs.get("sidebar")}

    @router.route("POST", "/api/prefs/sidebar")
    def set_sidebar(req):
        if len(req.raw_body) > MAX_BYTES:
            raise HttpError(413, "sidebar preferences over %d bytes"
                            % MAX_BYTES)
        sidebar = validate_sidebar(req.json().get("sidebar"))
        path = _path(req.server, req.user)
        prefs = _read(path)
        prefs["sidebar"] = sidebar
        _write(path, prefs)
        return 200, {"ok": True, "sidebar": sidebar}


register()
