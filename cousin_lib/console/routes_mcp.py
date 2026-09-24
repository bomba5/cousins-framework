"""Console routes for WP-D, MCP and policy: the per-cousin and install MCP registries, .mcp.json and policy.toml editors.

A package seam (app.py PACKAGE_ROUTE_MODULES): this module is the
package's own. Its routes are registered in register() below with
`@router.route(METHOD, "/api/...")`; per-server state lives on
req.server.state, never at module level. Long-running work goes through
console/longop.py and a pasted secret through console/secrets.py. The
browser side is mcp.jsx (index.html's package block)."""
from __future__ import annotations

from cousin_lib.console import router  # noqa: F401 - the package's routes use it


def register():
    """Nothing yet: the package adds its routes here."""


register()
