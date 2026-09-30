"""Plugin routes (docs/reference/console-api.md, "Plugins"; docs/plugins.md).

  GET  /api/plugins                  the install's plugins, their problems,
                                     each service's supervisor state and health
  GET  /api/cousins/<slug>/plugins   what the cousin enables, what it could
  POST /api/cousins/<slug>/plugins   {"enabled": [name, ...]}: cousin.toml
                                     [plugins] enabled (toml_edit), restart_required

and the proxy, outside /api/: `/plugins/<name>/<path...>` forwards GET,
POST and HEAD to the plugin's declared service port on 127.0.0.1, query
kept, behind the console's own guard and login (a session, as for every
/api/ route). The response is streamed chunk by chunk and flushed (an
event stream stays open), its status and Content-Type passed through;
hop-by-hop headers are dropped both ways, and so are the console's
cookie and any Authorization header on the way in. The request body is
bounded (MAX_BODY_BYTES) before it is read. A service that does not
answer is a 502 with one short line.

The framework never reads a plugin's own per-cousin settings; nothing
here writes anything but cousin.toml [plugins] enabled."""
from __future__ import annotations

import http.client
import urllib.request

from cousin_lib import plugins, supervisor
from cousin_lib.console import router
from cousin_lib.console.app import HttpError

MAX_BODY_BYTES = 1 << 20
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 60.0          # a response that is not an event stream
HEALTH_TIMEOUT_S = 1.0
CHUNK = 64 * 1024
METHODS = ("GET", "POST", "HEAD")
# RFC 9110 7.6.1, plus what a proxy must set itself
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate",
                        "proxy-authorization", "proxy-connection", "te", "trailer",
                        "trailers", "transfer-encoding", "upgrade", "host", "content-length"})
DROP_IN = frozenset({"cookie", "authorization"})
TMUX_GAP = ("the tmux kind reads .mcp.json itself: plugin MCP servers do not reach a tmux"
            " cousin in 2.1.0 (its console tab and service still work)")


def is_proxy_path(path):
    return path.startswith(plugins.PROXY_PREFIX)


# ---------------------------------------------------------------- the API

def _snapshot_rows(root):
    snap = supervisor.snapshot(root)
    children = (snap or {}).get("children")
    return children if isinstance(children, dict) else None


def _health(plugin):
    """True/False from the manifest's health path, None without one."""
    path = (plugin.service or {}).get("health")
    if not path:
        return None
    try:
        with urllib.request.urlopen(plugin.service_url + path, timeout=HEALTH_TIMEOUT_S) as r:
            return 200 <= r.status < 300
    except (OSError, ValueError, http.client.HTTPException):
        return False


def _enablers(root):
    """{plugin name: [slug, ...]} over the cousins under <root>/cousins."""
    out = {}
    base = root / "cousins"
    if not base.is_dir():
        return out
    for home in sorted(base.iterdir()):
        if (home / "cousin.toml").is_file():
            for name in plugins.cousin_enabled(home)[0]:
                out.setdefault(name, []).append(home.name)
    return out


def describe_install(root, *, probe=True):
    """GET /api/plugins's body."""
    loaded, problems = plugins.load(root)
    children = _snapshot_rows(root)
    enablers = _enablers(root)
    rows = []
    for name, plugin in loaded.items():
        row = plugin.row()
        row["enabled_by"] = enablers.get(name, [])
        row["health"] = (plugin.service or {}).get("health")
        if plugin.service:
            child = children.get("plugin:%s" % name) if children is not None else None
            row["supervisor"] = ({k: child.get(k) for k in ("state", "pid", "reason",
                                                             "restarts")}
                                 if isinstance(child, dict) else None)
            row["healthy"] = _health(plugin) if probe else None
        else:
            row["supervisor"] = row["healthy"] = None
        rows.append(row)
    return {"ok": True, "plugins": rows, "problems": problems,
            "supervisor": children is not None}


def cousin_plugins(root, home, loaded=None):
    """The fleet row's `plugins`: the cousin's valid enabled plugins, each
    {name, title, description, tab} where tab is {title, url, placement}
    or null."""
    loaded = loaded if loaded is not None else plugins.load(root)
    out = []
    for plugin in plugins.enabled_for(home, root, loaded):
        tab = plugin.console_tab(root, slug=home.name, home=home)
        out.append({"name": plugin.name, "title": (plugin.console or {}).get("title"),
                    "description": plugin.description,
                    "tab": ({k: tab[k] for k in ("title", "url", "placement")}
                            if tab else None)})
    return out


def describe_cousin(root, home):
    loaded = plugins.load(root)
    enabled, problem = plugins.cousin_enabled(home)
    _, skipped = plugins.cousin_report(home, root, loaded)
    from cousin_lib import agent_settings
    lane = agent_settings.summary(home).get("lane")
    return {"ok": True, "slug": home.name, "enabled": enabled, "problem": problem,
            "skipped": skipped,
            "available": [p.row() for p in loaded[0].values()],
            "plugins": cousin_plugins(root, home, loaded),
            "note": TMUX_GAP if lane == "tmux" else None}


def write_enabled(root, home, names):
    """cousin.toml [plugins] enabled = names (sorted); each must be a plugin
    the install has. Returns whether the file changed."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise HttpError(400, "enabled must be a list of plugin names")
    loaded, _ = plugins.load(root)
    unknown = sorted(set(names) - set(loaded))
    if unknown:
        raise HttpError(400, "not a plugin of this install: %s" % ", ".join(unknown),
                        errors={"enabled": "unknown: %s" % ", ".join(unknown)})
    want = sorted(set(names))
    current, problem = plugins.cousin_enabled(home)
    if problem is None and sorted(current) == want:
        return False
    from cousin_lib.console import toml_edit

    def check(parsed):
        table = parsed.get("plugins")
        if not isinstance(table, dict) or table.get("enabled") != want:
            raise ValueError("[plugins] enabled did not round-trip")
    try:
        toml_edit.write_keys(home, [("plugins", "enabled", want)], validate=check)
    except (TypeError, ValueError, OSError) as err:
        raise HttpError(400, str(err))
    return True


def _reload(root):
    """Ask the supervisor to rescan (a plugin service follows the cousins
    that enable it); None when none runs."""
    try:
        answer = supervisor.request(root, "reload", timeout=10.0)
    except supervisor.SupervisorUnavailable:
        return None
    return {k: answer.get(k) for k in ("ok", "added", "removed", "error") if k in answer}


def register():
    from cousin_lib.console._common import cousin_home

    @router.route("GET", "/api/plugins")
    def list_plugins(req):
        return 200, describe_install(req.server.root)

    @router.route("GET", "/api/cousins/{slug}/plugins")
    def get_cousin_plugins(req, slug):
        home = cousin_home(req.server, slug)
        return 200, describe_cousin(req.server.root, home)

    @router.route("POST", "/api/cousins/{slug}/plugins")
    def set_cousin_plugins(req, slug):
        server = req.server
        home = cousin_home(server, slug)
        if "enabled" not in req.body:
            raise HttpError(400, "enabled is required: a list of plugin names")
        from cousin_lib.console import longop
        try:
            hold = longop.exclusive(server, slug, "cousin settings")
        except longop.Busy as err:
            raise HttpError(409, str(err), busy=True)
        with hold:
            changed = write_enabled(server.root, home, req.body["enabled"])
        out = dict(describe_cousin(server.root, home), changed=changed,
                   restart_required=changed, reload=None)
        if changed:
            out["reload"] = _reload(server.root)
            from cousin_lib.console.routes_fleet import fleet_rows
            server.emit("cousins-refresh", fleet_rows(server))
        return 200, out


# ---------------------------------------------------------------- the proxy

def _refuse(handler, status, text):
    body = (text.rstrip("\n") + "\n").encode()
    handler.close_connection = True
    handler.send_bytes(status, body, {"Content-Type": "text/plain; charset=utf-8",
                                      "Cache-Control": "no-store"})


def serve_proxy(handler, method, path, query, length):
    """One /plugins/<name>/... request, answered on `handler` (app._Handler)."""
    server = handler.console
    if server.guard is not None and not server.guard(handler.client_address[0]):
        return handler.send_json(403, {"error": "address not allowed"})
    if length < 0 or length > MAX_BODY_BYTES:
        handler.close_connection = True
        return handler.send_json(413, {"error": "body too large"})
    body = handler.rfile.read(length) if length else b""
    refusal = handler.auth_refusal()
    if refusal is not None:
        return handler.send_json(*refusal)
    if method not in METHODS:
        return handler.send_json(405, {"error": "method not allowed"})
    name, _, rest = path[len(plugins.PROXY_PREFIX):].partition("/")
    if not plugins.NAME.match(name):
        return handler.send_json(404, {"error": "not found"})
    plugin = plugins.load(server.root)[0].get(name)
    if plugin is None or not plugin.service:
        return handler.send_json(404, {"error": "no plugin service named %s" % name})
    target = "/" + rest + ("?" + query if query else "")
    headers = {k: v for k, v in handler.headers.items()
               if k.lower() not in HOP_BY_HOP and k.lower() not in DROP_IN}
    headers["Host"] = "%s:%d" % (plugins.HOST, plugin.port)
    headers["X-Forwarded-Prefix"] = plugins.PROXY_PREFIX + name
    headers["Connection"] = "close"
    if method == "POST" or body:
        headers["Content-Length"] = str(len(body))
    conn = http.client.HTTPConnection(plugins.HOST, plugin.port, timeout=CONNECT_TIMEOUT_S)
    try:
        try:
            conn.request(method, target, body=body if method == "POST" or body else None,
                         headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as err:
            return _refuse(handler, 502, "plugin %s: its service on %s:%d does not answer (%s)"
                           % (name, plugins.HOST, plugin.port, type(err).__name__))
        _relay(handler, method, resp, conn)
    finally:
        conn.close()


def _relay(handler, method, resp, conn):
    """The upstream response, streamed: status and headers as they came
    (hop-by-hop dropped), then every chunk as it arrives, flushed."""
    ctype = resp.getheader("Content-Type") or ""
    streaming = ctype.split(";")[0].strip().lower() == "text/event-stream"
    handler.send_response(resp.status)
    for key, value in resp.getheaders():
        if key.lower() not in HOP_BY_HOP:
            handler.send_header(key, value)
    length = resp.getheader("Content-Length")
    if length is not None and not resp.chunked:
        handler.send_header("Content-Length", length)
    if streaming:
        handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = True
    if method == "HEAD":
        return
    try:
        if conn.sock is not None:
            # an event stream may be quiet for as long as it likes
            conn.sock.settimeout(None if streaming else READ_TIMEOUT_S)
        while True:
            chunk = resp.read1(CHUNK)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (OSError, http.client.HTTPException):
        pass                    # either side went away: the stream is over


register()
