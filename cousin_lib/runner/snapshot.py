"""What the CLI's prompt snapshot freezes, as one hash (#33).

The sdk runner passes `snapshot: True` (prompt.system_prompt_option), so
a session records its system prompt and its tool list on its first
request and sends that record on every resume. `fingerprint` hashes the
same three things the runner serves: the composed prompt with the
version held fixed (a release alone moves only the contract's header
line), the registry's tool definitions, and the cousin's own MCP servers.
The runner saves it with the session (runner-session.json `snapshot`)
and rolls a resume over when it moved; the loops daemon reads it to flip
an idle generation whose snapshot is stale (#280). No SDK import here."""
import hashlib
import json


def fingerprint(home, root, *, registry=None, servers=None):
    """The hash, or None when it cannot be built. `registry` and `servers`
    are what a running runner already holds; without them they are read
    the way a runner start reads them."""
    try:
        from cousin_lib.runner import mcp_config, prompt, tools
        if registry is None:
            registry = tools.resolve_registry(home, root)[0]
        if servers is None:
            servers = mcp_config.load(home, root=root).servers
        text = json.dumps({"prompt": prompt.compose_system_prompt(
                               home, root=root, registry=registry, version="0"),
                           "tools": tools.tool_definitions(registry),
                           "mcp": servers}, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - a fingerprint must never fail a start
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
