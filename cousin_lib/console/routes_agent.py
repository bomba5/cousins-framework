"""Console routes for WP-A, agent and cousin settings: every [agent] key through cousin_lib.agent_settings (describe, validate, apply) and the harness [agent] defaults.

A package seam (app.py PACKAGE_ROUTE_MODULES): this module is the
package's own. Its routes are registered in register() below with
`@router.route(METHOD, "/api/...")`; per-server state lives on
req.server.state, never at module level. Long-running work goes through
console/longop.py and a pasted secret through console/secrets.py. The
browser side is agent.jsx (index.html's package block)."""
from __future__ import annotations

from cousin_lib.console import router  # noqa: F401 - the package's routes use it


def register():
    """Nothing yet: the package adds its routes here."""


register()
