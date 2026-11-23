"""What a cousin's own MCP servers serve, listed by the runner itself.

The agent CLI records the tool definitions on a session's first request
(the preset's `snapshot`) and sends that record on every resume. A server
whose code changed behind the same `.mcp.json` line moves no fingerprint
(snapshot.fingerprint counts the file's text), so a restarted cousin kept
the old tool. The CLI's own MCP status reports each tool's name and
annotations only (measured with CLI 2.1.321: no description, no input
schema), so it cannot see a reworded tool or a new parameter.

So the runner asks each server itself, the way the CLI starts it (the
same command, arguments, environment and working directory, or the same
URL and headers, `${NAME}` expanded as the CLI expands it), and hashes every tool's name, description, input schema and
annotations. `served` gives {server: hash}; a server it cannot list in
TIMEOUT seconds, or of a kind it does not speak, is left out: a broken
server must never read as a change on every restart, and a check that
cannot list fails open."""
import asyncio
import hashlib
import json
import os

TIMEOUT = 20.0


def digest(tools):
    """One hash of a server's tools: [{name, description, inputSchema,
    annotations}], in name order."""
    rows = sorted(([t.get("name") or "", t.get("description") or "", t.get("inputSchema") or {},
                    t.get("annotations") or {}] for t in tools), key=lambda row: row[0])
    text = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _tool(t):
    ann = t.annotations.model_dump(exclude_none=True) if getattr(t, "annotations", None) else {}
    return {"name": t.name, "description": t.description, "inputSchema": t.inputSchema, "annotations": ann}


async def list_one(spec, cwd, environ=None):
    """The digest of one server's tools; None for a kind it does not speak.
    `environ` is the CLI's environment (default this process's): the sdk
    runner adds the cousin's [agent] env_secrets to it."""
    from mcp import ClientSession

    from cousin_lib.runner.mcp_config import expand
    # `${NAME}` is the CLI's to expand (mcp_config passes it through), so the
    # listing expands it the same way, or a server would start without its
    # credential and be left out every time
    environ = os.environ if environ is None else environ
    x = lambda value: expand(str(value), environ)  # noqa: E731
    kind = spec.get("type") or ("stdio" if spec.get("command") else "http" if spec.get("url") else None)
    if kind == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = dict(environ, **{k: x(v) for k, v in (spec.get("env") or {}).items()})
        params = StdioServerParameters(command=x(spec["command"]), args=[x(a) for a in spec.get("args") or ()],
                                       env=env, cwd=str(cwd))
        with open(os.devnull, "w") as quiet:
            async with stdio_client(params, errlog=quiet) as (read, write):
                return await _list(ClientSession, read, write)
    if kind == "http":
        from mcp.client.streamable_http import streamablehttp_client
        headers = {k: x(v) for k, v in (spec.get("headers") or {}).items()} or None
        async with streamablehttp_client(x(spec["url"]), headers=headers) as (read, write, _):
            return await _list(ClientSession, read, write)
    if kind == "sse":
        from mcp.client.sse import sse_client
        headers = {k: x(v) for k, v in (spec.get("headers") or {}).items()} or None
        async with sse_client(x(spec["url"]), headers=headers) as (read, write):
            return await _list(ClientSession, read, write)
    return None


async def _list(session_cls, read, write):
    async with session_cls(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
    return digest([_tool(t) for t in tools])


async def served(servers, cwd, environ=None):
    """{server name: digest} for every server it could list, in parallel,
    each started with `environ` (list_one)."""
    async def one(name, spec):
        try:
            return name, await asyncio.wait_for(list_one(spec, cwd, environ), TIMEOUT)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:  # noqa: BLE001 - a server that cannot be listed (an anyio group too) is left out
            return name, None
    pairs = await asyncio.gather(*(one(n, s) for n, s in (servers or {}).items() if isinstance(s, dict)))
    return {name: h for name, h in pairs if h is not None}
