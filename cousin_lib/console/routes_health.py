"""The health route (docs/reference/console-api.md, "Health"): a read of
cousin_lib.health.summary, the record the loops daemon writes after each
tick plus the supervisor's children that are not running. Read-only; the
topbar's failing count (app.jsx HealthBadge) polls it."""
from __future__ import annotations

from cousin_lib import health
from cousin_lib.console import router


def register():
    @router.route("GET", "/api/health")
    def get_health(req):
        body = health.summary(req.server.root)
        return 200, dict(body, failing_count=health.failing_count(body))


register()
