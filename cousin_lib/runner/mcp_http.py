"""The runner's tools as a remote MCP server on loopback (phase 9 R7).

The SDK lane serves the framework's tools in process
(tools.build_tool_server). opencode cannot load a Python server into
itself, but it speaks MCP to a remote server (`{type: "remote", url,
headers}`), so the runner serves the same tools itself over MCP
streamable HTTP, on 127.0.0.1, behind a bearer token that is fresh for
every start. Every tools/call goes to tools.call(ctx, name, args) in
the runner's process with the runner's own ToolContext: `reply` reads
the live Turn, `handoff` reaches the runner's handoff box, and a tool's
behaviour lives in one place for both lanes. A separate cousin-mcp
process would have none of that.

The transport is the plain half of streamable HTTP
(modelcontextprotocol.io, 2025-06-18, "Transports"): one endpoint,
`/mcp`; a JSON-RPC request is a POST answered with one
application/json object; a notification or a response is a POST
answered 202 with no body; a GET is 405 (this server never starts a
stream of its own); no Mcp-Session-Id (sessions are optional and this
server has no per-client state). The Origin header, when present, must
be a loopback origin (the spec's DNS-rebinding rule). Handlers run on
the HTTP server's threads, as the SDK lane's run on asyncio.to_thread
workers: tools.call is synchronous and Turn is locked.

Stdlib only."""
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from cousin_lib import mcp_server
from cousin_lib.runner import tools

PATH = "/mcp"
# Newest first. initialize echoes the client's version when it is one of
# these, else answers the newest (the spec's version negotiation).
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_VERSION = "1.0.0"
MAX_BODY = 4 * 1024 * 1024
TOKEN_BYTES = 32

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


class _Handler(BaseHTTPRequestHandler):
    server_version = "cousin-mcp-http/" + SERVER_VERSION
    # HTTP/1.0: one exchange per connection, so stop() leaves no idle
    # keep-alive connection behind.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):        # the runner's stream is the log, not stderr
        pass

    # -------------------------------------------------------------- replies

    def _send(self, status, body=b"", content_type=None, headers=()):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), "application/json")

    # ----------------------------------------------------------------- gate

    def _gate(self):
        """True when this request may reach the endpoint; otherwise the
        refusal has been sent. Order: path, token, origin, version."""
        if urlsplit(self.path).path != PATH:
            self._send(404)
            return False
        owner = self.server.owner
        given = self.headers.get("Authorization") or ""
        scheme, _sp, value = given.partition(" ")
        if scheme.lower() != "bearer" or not owner.token or not hmac.compare_digest(
                value.strip().encode(), owner.token.encode()):
            self._send(401, headers=[("WWW-Authenticate", 'Bearer realm="cousin"')])
            return False
        origin = self.headers.get("Origin")
        if origin is not None and (urlsplit(origin).hostname or "") not in _LOOPBACK:
            self._send(403)
            return False
        version = self.headers.get("MCP-Protocol-Version")
        if version is not None and version not in PROTOCOL_VERSIONS:
            self._json(400, _error(None, INVALID_REQUEST,
                                   "unsupported MCP-Protocol-Version %r" % version))
            return False
        return True

    # -------------------------------------------------------------- methods

    def do_GET(self):
        if self._gate():
            self._send(405, headers=[("Allow", "POST")])

    def do_DELETE(self):
        if self._gate():
            self._send(405, headers=[("Allow", "POST")])

    def do_POST(self):
        # The body is read before any refusal: closing a socket with unread
        # data resets it, and the client would see a reset, not the 401.
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411)
            return
        if length < 0 or length > MAX_BODY:
            self._send(413)
            return
        raw = self.rfile.read(length)
        if not self._gate():
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, _error(None, PARSE_ERROR, "parse error"))
            return
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            self._json(400, _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message"))
            return
        if "method" not in msg or "id" not in msg:
            # A notification, or a response to a request this server never
            # sends: accepted, nothing to answer.
            self._send(202)
            return
        if not isinstance(msg["method"], str):
            self._json(400, _error(msg.get("id"), INVALID_REQUEST, "method must be a string"))
            return
        self._json(200, self.server.owner._handle(msg))


class McpHttpServer:
    """The tool registry over MCP streamable HTTP, on loopback, for one
    runner. `registry` None resolves it as build_tool_server does (the
    cousin's, the install's, else the shipped default, said on ctx.stream
    or to on_fallback); a registry command with no handler is a
    RunnerError here, before a port is bound. `token` None: a fresh
    secrets token at every start()."""

    def __init__(self, ctx, registry=None, *, host="127.0.0.1", port=0, token=None,
                 on_fallback=None):
        if registry is None:
            registry, notice = tools.resolve_registry(ctx.home, ctx.root)
            if notice is not None:
                if on_fallback is not None:
                    on_fallback(notice)
                elif ctx.stream is not None:
                    ctx.stream.append("policy", notice)
        tools.check_registry(registry)
        ctx.registry = registry
        self.ctx = ctx
        self.registry = registry
        self.host = host
        self.port = port
        self._fixed_token = token
        self.token = token
        self._tools = [{"name": d["name"], "description": d["description"],
                        "inputSchema": d["inputSchema"]}
                       for d in tools.tool_definitions(registry)]
        self._httpd = None
        self._thread = None

    @property
    def url(self):
        if self._httpd is None:
            return None
        host, port = self._httpd.server_address[:2]
        return "http://%s:%d%s" % (host, port, PATH)

    def start(self):
        if self._httpd is not None:
            return
        self.token = self._fixed_token or secrets.token_urlsafe(TOKEN_BYTES)
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True
        httpd.owner = self
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="cousin-mcp-http",
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def stop(self):
        httpd, thread = self._httpd, self._thread
        self._httpd = self._thread = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)

    # ------------------------------------------------------------ JSON-RPC

    def _handle(self, msg):
        """One JSON-RPC request -> its response object. Never raises."""
        rid, method = msg.get("id"), msg["method"]
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(rid, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(rid, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": mcp_server.SERVER_NAME, "version": SERVER_VERSION}})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": self._tools})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments")
            if args is None:
                args = {}
            if not isinstance(name, str) or not name:
                return _error(rid, INVALID_PARAMS, "tools/call needs a tool name")
            if not isinstance(args, dict):
                return _error(rid, INVALID_PARAMS, "tools/call arguments must be an object")
            return _result(rid, self._call(name, args))
        return _error(rid, METHOD_NOT_FOUND, "method not found: %s" % method)

    def _call(self, name, args):
        try:
            text, is_error = tools.call(self.ctx, name, args)
        except Exception as err:  # noqa: BLE001 - a broken call is a tool error, never a crash
            text, is_error = "%s: %s" % (type(err).__name__, err), True
        return {"content": [{"type": "text", "text": "" if text is None else str(text)}],
                "isError": bool(is_error)}
