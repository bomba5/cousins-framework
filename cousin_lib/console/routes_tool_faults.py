"""The tool faults route (docs/reference/console-api.md, "Tool faults"): the
rows each local cousin's runner wrote to data/tool-faults.jsonl
(runner/tool_faults.py), counted by cousin and kind, for the System page's
tool faults tab. Read-only."""
from __future__ import annotations

from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router
from cousin_lib.console._common import cousin_home
from cousin_lib.console.app import HttpError
from cousin_lib.runner import tool_faults

MAX_DAYS = 90
MAX_LIMIT = 1000


def register():
    @router.route("GET", "/api/tool-faults")
    def get_tool_faults(req):
        days = req.int_query("days", 7)
        limit = req.int_query("limit", 200)
        kind = req.query.get("kind") or None
        slug = req.query.get("cousin") or None
        if not 1 <= days <= MAX_DAYS:
            raise HttpError(400, "days must be 1 to %d" % MAX_DAYS)
        if not 1 <= limit <= MAX_LIMIT:
            raise HttpError(400, "limit must be 1 to %d" % MAX_LIMIT)
        if kind is not None and kind not in tool_faults.KINDS:
            raise HttpError(400, "unknown kind %s" % kind)
        if slug:
            homes = {slug: cousin_home(req.server, slug)}
        else:
            homes = {c.slug: c.home for c in FrameworkConfig(req.server.root).list_cousins()}
        return 200, tool_faults.report(homes, days=days, kind=kind, limit=limit)


register()
