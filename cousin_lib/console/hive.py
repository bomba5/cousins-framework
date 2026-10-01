"""The console as the hive's queen (docs/reference/hive-api.md, "The console
is the queen"; docs/remote-cousins.md).

Off unless config/hive.toml says `enabled = true`: then, and only then,
the console's own server answers the queen routes under /hive/, lists
remote cousins beside the local ones, and can build a node archive.
Absent, disabled or unusable, every /hive/ path is a 404, no remote
row appears, and nothing under shared/hive is created.

Two authentication worlds that never mix:

- /hive/* is for nodes. A node has no console session, so these paths
  bypass the network guard and the operator login and authenticate
  with the hive bearer token alone (cousin_lib.hive.handle_request;
  identity is the token's slug, never the body). The one-time archive
  download under /hive/download/<nonce> is authenticated by its
  unguessable, single-use nonce.
- /api/* is for the operator (cookie session, network guard). No
  operator route reads an Authorization header, so a hive token is
  worth nothing there.

What the operator routes add (behind the normal console auth):
`GET /api/hive` (is the hive on, and its settings), `POST
/api/hive/nodes` (build an archive; answers a one-time download URL
and the install commands), `POST /api/hive/nodes/<slug>/revoke`, and
`DELETE /api/hive/nodes/<slug>` (forget a revoked node).
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from secrets import token_urlsafe

from cousin_lib import hive as hive_lib
from cousin_lib.console import router
from cousin_lib.console.app import HttpError

DOWNLOAD_TTL_SECONDS = 15 * 60
# The largest /hive/ request body the console reads; the door is in
# front of every other check.
MAX_BODY_BYTES = hive_lib.MAX_BODY_BYTES
DEFAULT_NODE_PORT = 8210
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


# ---- settings, store, context --------------------------------------------

def settings(server):
    """The parsed config/hive.toml, or None when the hive is off. A
    broken file reads as off (fail closed) and is reported to stderr
    once per distinct error."""
    try:
        cfg = hive_lib.hive_config(server.root)
    except hive_lib.HiveConfigError as err:
        text = str(err)
        if server.state.get("hive_config_error") != text:
            server.state["hive_config_error"] = text
            print("cousin-console: hive off: %s" % text, file=sys.stderr,
                  flush=True)
        return None
    server.state.pop("hive_config_error", None)
    return cfg


def store(server):
    """The queen's store under <root>/shared/hive, opened once per
    server. Only called when the hive is on."""
    lock = server.state.setdefault("hive_lock", threading.Lock())
    with lock:
        existing = server.state.get("hive_store")
        if existing is None:
            existing = hive_lib.HiveStore(server.root / "shared" / "hive")
            server.state["hive_store"] = existing
        return existing


def context(server, cfg):
    """The QueenContext for this server, rebuilt when the checkin
    period in hive.toml changes."""
    ctx = server.state.get("hive_context")
    period, home = cfg["checkin_seconds"], cfg.get("home_cousin") or ""
    if ctx is None or ctx.checkin_seconds != period or getattr(ctx, "home_cousin", "") != home:
        ctx = hive_lib.QueenContext(
            store(server), checkin_seconds=period,
            embedder=hive_lib.EmbedderSource(server.root), min_score=None,
            on_checkin=lambda slug, previous: _on_checkin(
                server, slug, previous),
            tell_home=(lambda slug, body: tell_home(server, home, slug, body)) if home else None)
        ctx.home_cousin = home
        server.state["hive_context"] = ctx
    return ctx


def tell_home(server, home, slug, body):
    """A node's tell-home, authenticated by its token (`slug`): through
    peer_inbound.accept to the home cousin only, under the name the
    operator minted the token with (else the slug), never the name the
    node checks in with. Returns (status, payload)."""
    from cousin_lib import peer_inbound
    display = store(server).minted_name(slug) or slug
    try:
        out = peer_inbound.accept(
            server.root, identity="hive:%s" % slug, display=display,
            to=home, message=body.get("message"), msg_id=body.get("msg_id"),
            sent_at=body.get("sent_at"), allowed=lambda target: target == home)
    except peer_inbound.Refused as err:
        return err.status, {"error": err.error}
    return 200, {"ok": True, "to": home, "id": out.get("id")}


def _on_checkin(server, slug, previous):
    """A node that comes (back) online updates its card at once; going
    offline is a matter of time and the fleet refresh shows it."""
    cfg = settings(server)
    if cfg is None:
        return
    if previous is not None and hive_lib.is_online(
            previous, cfg["checkin_seconds"]):
        return

    def push():
        from cousin_lib.console import routes_fleet
        try:
            server.emit("cousins-refresh", routes_fleet.fleet_rows(server))
        except Exception:  # noqa: BLE001 - an event is best-effort
            pass

    threading.Thread(target=push, daemon=True).start()


# ---- the /hive/ door -------------------------------------------------------

def is_hive_path(path):
    return path == "/hive" or path.startswith("/hive/")


def serve(handler, method, path, query, raw_body):
    """Answer one /hive/ request on the console's handler. Called
    before the network guard and the login check: a node authenticates
    with its token (or a download nonce), never a session."""
    server = handler.console
    cfg = settings(server)
    if cfg is None:
        handler.send_json(404, {"error": "not found"})
        return
    if method == "GET" and path.startswith("/hive/download/"):
        _serve_download(handler, server, path[len("/hive/download/"):])
        return
    if method not in ("GET", "POST"):
        handler.send_json(404, {"error": "not found"})
        return
    try:
        status, payload = hive_lib.handle_request(
            context(server, cfg), method, path, query, handler.headers,
            raw_body, handler.client_address[0])
    except Exception as err:  # noqa: BLE001 - the wire needs a body
        status, payload = 500, {"error": "%s: %s"
                                         % (type(err).__name__, err)}
    handler.send_json(status, payload)


# ---- one-time archive downloads ------------------------------------------

def _downloads(server):
    return server.state.setdefault("hive_downloads", {})


def _downloads_lock(server):
    return server.state.setdefault("hive_downloads_lock", threading.Lock())


def _discard(entry):
    shutil.rmtree(entry["dir"], ignore_errors=True)


def downloads_dir(server):
    """<root>/shared/hive/downloads, owner-only: where built archives
    wait for their one download. Each build gets its own mode-0700
    subdirectory."""
    path = server.root / "shared" / "hive" / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def purge_expired(server, *, now=None):
    """Drop every download past its expiry, deleting its file. Runs on
    each build and each download, and from a timer at each expiry. It
    also sweeps build directories no table entry names once they are
    older than the link's lifetime: what a console killed before its
    stop hook ran (a SIGTERM) left behind."""
    now = time.time() if now is None else now
    with _downloads_lock(server):
        table = _downloads(server)
        for nonce in [n for n, e in table.items() if e["expires"] <= now]:
            _discard(table.pop(nonce))
        known = {e["dir"] for e in table.values()}
    base = server.root / "shared" / "hive" / "downloads"
    try:
        leftovers = [p for p in base.iterdir() if p.is_dir()
                     and p not in known]
    except OSError:
        return
    for path in leftovers:
        try:
            stale = now - path.stat().st_mtime > DOWNLOAD_TTL_SECONDS
        except OSError:
            continue
        if stale:
            shutil.rmtree(path, ignore_errors=True)


def cleanup(server):
    """Server stop: every pending archive is deleted and the store
    closed."""
    opened = server.state.pop("hive_store", None)
    server.state.pop("hive_context", None)
    if opened is not None:
        opened.close()
    with _downloads_lock(server):
        table = _downloads(server)
        for nonce in list(table):
            entry = table.pop(nonce)
            timer = entry.get("timer")
            if timer is not None:
                timer.cancel()
            _discard(entry)


def _register_download(server, tarball, directory, slug):
    nonce = token_urlsafe(32)
    expires = time.time() + DOWNLOAD_TTL_SECONDS
    timer = threading.Timer(DOWNLOAD_TTL_SECONDS + 1,
                            lambda: purge_expired(server))
    timer.daemon = True
    with _downloads_lock(server):
        _downloads(server)[nonce] = {
            "path": Path(tarball), "dir": Path(directory), "slug": slug,
            "expires": expires, "timer": timer}
    timer.start()
    return nonce, expires


def _serve_download(handler, server, nonce):
    """The archive once: the entry is taken out of the table before a
    byte is sent, so a second request (or a racing one) is a 404; the
    file is deleted after it is read."""
    purge_expired(server)
    entry = None
    if _NONCE_RE.match(nonce or ""):
        with _downloads_lock(server):
            entry = _downloads(server).pop(nonce, None)
    if entry is None:
        handler.send_json(404, {"error": "not found"})
        return
    timer = entry.get("timer")
    if timer is not None:
        timer.cancel()
    try:
        payload = entry["path"].read_bytes()
    except OSError:
        _discard(entry)
        handler.send_json(404, {"error": "not found"})
        return
    _discard(entry)
    handler.send_bytes(200, payload, {
        "Content-Type": "application/gzip",
        "Content-Disposition": 'attachment; filename="%s"'
                               % entry["path"].name,
        "Cache-Control": "no-store"})


# ---- remote rows -------------------------------------------------------------

def remote_row(node, checkin_seconds, *, now=None):
    """One remote cousin card from a nodes() row. Carries every key a
    local fleet row has (the views read them), with the local-only ones
    null, plus the remote facts: host, port, lastSeen, online, state."""
    now = time.time() if now is None else now
    online = (node["checked_in"] and not node["revoked"]
              and hive_lib.is_online(node["last_seen"], checkin_seconds,
                                     now=now))
    if node["revoked"]:
        state = "revoked"
    elif not node["checked_in"]:
        state = "pending"
    else:
        state = "online" if online else "offline"
    return {
        "slug": node["slug"], "name": node["name"], "role": node["role"],
        "type": "remote", "remote": True, "remoteState": state,
        "online": bool(online), "revoked": bool(node["revoked"]),
        "checkedIn": bool(node["checked_in"]),
        "host": node["host"], "port": node["port"],
        "version": node["version"], "lastSeen": node["last_seen"],
        "status": "running" if online else "stopped",
        "chat": "ok" if online else ("none" if not node["checked_in"]
                                     else "down"),
        "home": None, "tmuxSession": None, "operator": None,
        "memoryScope": None, "heartbeat": None, "flipAt": None,
        "model": None, "effort": None, "hidden": False, "auth": None,
        "attention": None, "active": False, "pid": None,
        "uptime_seconds": None, "activity": "", "lastMsgTs": 0,
        "tokensSpent": 0,
    }


def remote_rows(server, local_slugs=()):
    """Cards for every node the queen knows, or [] when the hive is off
    (and then the store is never opened). A slug that is also a local
    cousin stays local: the local row wins."""
    cfg = settings(server)
    if cfg is None:
        return []
    local = set(local_slugs)
    now = time.time()
    return [remote_row(node, cfg["checkin_seconds"], now=now)
            for node in store(server).nodes() if node["slug"] not in local]


class RemoteCousin:
    """The duck-type the chat proxy reads for a remote node: where its
    chat server is, and the bearer the console presents to it (the
    node's own token; the node accepts a non-loopback request only
    with it)."""

    remote = True
    home = None
    type = "remote"

    def __init__(self, slug, name, host, port, token):
        self.slug = slug
        self.name = name
        self.chat_host = host
        self.chat_port = port
        self.auth_token = token


def find_remote(server, slug):
    """(RemoteCousin, None), (None, (status, error)) for a known node
    that cannot be chatted with, or (None, None) when the slug is not
    a node or the hive is off."""
    if server is None or settings(server) is None:
        return None, None
    node = None
    for row in store(server).nodes():
        if row["slug"] == slug:
            node = row
            break
    if node is None:
        return None, None
    if node["revoked"]:
        return None, (404, "remote cousin %s is revoked" % slug)
    if not node["checked_in"] or not node["port"]:
        return None, (502, "remote cousin %s has not checked in yet" % slug)
    return RemoteCousin(slug, node["name"], node["host"], node["port"],
                        store(server).token_for(slug)), None


# ---- operator routes -------------------------------------------------------

def _require_on(server):
    cfg = settings(server)
    if cfg is None:
        raise HttpError(404, "not found")
    return cfg


def _local_slugs(server):
    from cousin_lib.config import FrameworkConfig
    return {c.slug for c in FrameworkConfig(server.root).list_cousins()}


def _refresh(server):
    from cousin_lib.console import routes_fleet
    try:
        server.emit("cousins-refresh", routes_fleet.fleet_rows(server))
    except Exception:  # noqa: BLE001
        pass


def build_request(server, cfg, body):
    """Validate the dialog's body and build the archive into a private
    temp dir; returns the response body. Every refusal is an HttpError
    raised before anything is minted or written."""
    from cousin_lib import spawn_node

    slug = body.get("slug")
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise HttpError(400, "slug must match ^[a-z][a-z0-9_-]{1,31}$")
    if slug in _local_slugs(server):
        raise HttpError(409, "%s is already a local cousin" % slug)
    name = body.get("name") or slug.capitalize()
    role = body.get("role")
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise HttpError(400, "name must be 1-64 characters")
    if not isinstance(role, str) or not role.strip() or len(role) > 500:
        raise HttpError(400, "role is required (at most 500 characters)")
    port = body.get("port", DEFAULT_NODE_PORT)
    if port in (None, ""):
        port = DEFAULT_NODE_PORT
    if isinstance(port, bool) or not isinstance(port, int) \
            or not 0 < port < 65536:
        raise HttpError(400, "port must be an integer 1-65535")
    brain = body.get("brain", "placeholder")
    agent_cmd = ""
    if brain == "agent":
        agent_cmd = body.get("agent_cmd")
        if not isinstance(agent_cmd, str) or not agent_cmd.strip():
            raise HttpError(400, "the agent brain needs its command line")
        agent_cmd = agent_cmd.strip()
    elif brain != "placeholder":
        raise HttpError(400, "brain must be placeholder or agent")
    home_chat = body.get("home_chat", False)
    reachable = body.get("reachable", True)
    if not isinstance(home_chat, bool) or not isinstance(reachable, bool):
        raise HttpError(400, "home_chat and reachable must be booleans")
    tell_home = bool(home_chat and cfg.get("home_cousin"))
    if home_chat and not tell_home:
        raise HttpError(400, "home chat needs home_cousin in config/hive.toml")
    purge_expired(server)
    directory = tempfile.mkdtemp(prefix="node-",
                                 dir=downloads_dir(server))  # mode 0700
    try:
        result = spawn_node.build_node_archive(
            server.root, slug=slug, queen_url=cfg["public_url"],
            name=name.strip(), role=role.strip(), out=directory,
            tell_home=tell_home,
            port=port, agent_cmd=agent_cmd,
            node_host="0.0.0.0" if reachable else "127.0.0.1")
    except spawn_node.SpawnNodeError as err:
        shutil.rmtree(directory, ignore_errors=True)
        raise HttpError(400, str(err))
    nonce, expires = _register_download(server, result["tarball"],
                                        directory, slug)
    url = "%s/hive/download/%s" % (cfg["public_url"], nonce)
    top = "%s-node" % slug
    install = "tar xzf %s.tar.gz && cd %s && ./install.sh" % (top, top)
    _refresh(server)
    return {
        "ok": True, "slug": slug, "download_url": url,
        "download_path": "/hive/download/%s" % nonce,
        "expires_at": expires, "expires_in": DOWNLOAD_TTL_SECONDS,
        "filename": "%s.tar.gz" % top, "install": install,
        "curl": "curl -fsSL -o %s.tar.gz '%s' && %s" % (top, url, install),
        "note": "the archive carries the node's bearer token: the link"
                " works once and expires in 15 minutes",
    }


def register():
    @router.route("GET", "/api/hive")
    def hive_status(req):
        server = req.server
        cfg = settings(server)
        if cfg is None:
            out = {"enabled": False}
            if server.state.get("hive_config_error"):
                out["error"] = server.state["hive_config_error"]
            return 200, out
        return 200, {"enabled": True, "public_url": cfg["public_url"],
                     "checkin_seconds": cfg["checkin_seconds"],
                     "home_cousin": cfg["home_cousin"] or None,
                     "default_port": DEFAULT_NODE_PORT}

    @router.route("GET", "/api/hive/nodes")
    def hive_nodes(req):
        _require_on(req.server)
        return 200, {"nodes": remote_rows(req.server)}

    @router.route("POST", "/api/hive/nodes")
    def hive_build(req):
        cfg = _require_on(req.server)
        return 201, build_request(req.server, cfg, req.body)

    @router.route("POST", "/api/hive/nodes/{slug}/revoke")
    def hive_revoke(req, slug):
        _require_on(req.server)
        if not _SLUG_RE.match(slug):
            raise HttpError(400, "bad slug")
        count = store(req.server).revoke(slug)
        if not count:
            raise HttpError(404, "%s has no live token" % slug)
        _refresh(req.server)
        return 200, {"ok": True, "slug": slug, "revoked": count}

    @router.route("DELETE", "/api/hive/nodes/{slug}")
    def hive_forget(req, slug):
        _require_on(req.server)
        if not _SLUG_RE.match(slug):
            raise HttpError(400, "bad slug")
        try:
            out = store(req.server).forget(slug)
        except ValueError as err:
            raise HttpError(409, str(err))
        if not out["tokens"] and not out["nodes"]:
            raise HttpError(404, "unknown node %s" % slug)
        _refresh(req.server)
        return 200, {"ok": True, "slug": slug, **out}


register()
