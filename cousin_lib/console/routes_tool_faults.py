"""The tool faults route (docs/reference/console-api.md, "Tool faults"): the
rows each local cousin's runner wrote to data/tool-faults.jsonl
(runner/tool_faults.py), counted by cousin and kind, for the System page's
tool faults tab. Read-only.

Privacy: as on the skills routes, a System route never shows a cousin that
loads the `private` repository: a row holds its commands and errors."""
from __future__ import annotations

from cousin_lib import skills
from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router
from cousin_lib.console.app import HttpError
from cousin_lib.runner import tool_faults
from cousin_lib.skills import SkillError

MAX_DAYS = 90
MAX_LIMIT = 1000


def _homes(server):
    """{slug: home} of the cousins this route may show."""
    out = {}
    for config in FrameworkConfig(server.root).list_cousins():
        try:
            if "private" not in skills.cousin_repos(config.home):
                out[config.slug] = config.home
        except SkillError:
            continue        # its list does not parse: it may name the private repository
    return out


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
        homes = _homes(req.server)
        if slug and slug not in homes:
            raise HttpError(404, "unknown cousin %s" % slug)
        shown = {slug: homes[slug]} if slug else homes
        body = tool_faults.report(shown, days=days, kind=kind, limit=limit)
        return 200, dict(body, cousins=sorted(homes))


register()
