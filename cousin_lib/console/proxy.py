"""Chat routes: a proxy over each cousin's chat server
(docs/console-spec.md, "Chat: a proxy over each cousin's chat server").

Every route names the cousin, resolves its `host`/`port` from the
filesystem registry on that call, and forwards to the routes in
docs/chat-server-spec.md. The console stores no message: the only file
it touches under a cousin home is the inbox it serves read-only, and
the attachment annotation on history rows is one directory listing per
request, a projection and not a store.

Error mapping: `400 {"error": "bad slug"}` before any lookup, `404`
unknown cousin, `502 {"ok": false, "error": ...}` when the server is
unreachable, has no port, or answers non-JSON; any JSON answer from the
server passes through with its status (an upstream `400` keeps its
body).

The request object the handlers read is the server's: `req.root` (the
framework root, a path or a FrameworkConfig; absent means
`FRAMEWORK_ROOT`), `req.query` (a mapping of the first value per query
parameter) and `req.body` (the parsed JSON object; the server has
already turned malformed JSON into its 400).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router
from cousin_lib.console.static import RawResponse  # noqa: F401 - re-exported

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_INBOX_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_INBOX_NAME_RE = re.compile(r"^(\d+)\.(png|jpg|jpeg|gif|webp)$")
_READ_TIMEOUT = 5.0
_SEND_TIMEOUT = 15.0   # an image can be megabytes


class RouteError(Exception):
    """A refused request: carries the (status, body) pair to return."""

    def __init__(self, status, body):
        super().__init__(status, body)
        self.status, self.body = status, body


def framework(req):
    root = getattr(req, "root", None)
    if isinstance(root, FrameworkConfig):
        return root
    return FrameworkConfig.resolve(str(root) if root else None)


def find_cousin(req, slug):
    """The cousin's CousinConfig, or a RouteError (400 bad slug, 404
    unknown)."""
    if not slug or not SLUG_RE.match(slug):
        raise RouteError(400, {"ok": False, "error": "bad slug"})
    for cousin in framework(req).list_cousins():
        if cousin.slug == slug:
            return cousin
    raise RouteError(404, {"ok": False, "error": "unknown cousin"})


def _upstream(cousin, path, *, query=None, body=None, timeout=_READ_TIMEOUT):
    """(status, json) from the cousin's server. Anything that is not a
    JSON answer from a reachable server is a 502 here."""
    if not cousin.chat_port:
        raise RouteError(502, {"ok": False,
                               "error": "cousin has no chat port configured"})
    host = cousin.chat_host or "127.0.0.1"
    url = "http://%s:%d%s" % (host, cousin.chat_port, path)
    if query:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    except (urllib.error.URLError, OSError) as err:
        raise RouteError(502, {"ok": False,
                               "error": "chat server unreachable: %s"
                                        % getattr(err, "reason", err)})
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise RouteError(502, {"ok": False,
                               "error": "chat server answered non-JSON"})
    return status, parsed


def _require(mapping, *keys):
    missing = [k for k in keys if not mapping.get(k)]
    if missing:
        raise RouteError(400, {"ok": False,
                               "error": "%s required" % " and ".join(missing)})


def _inbox_files(cousin):
    """{message id: file name} for the cousin's inbox, one listing."""
    inbox = cousin.home / "chat" / "inbound"
    out = {}
    try:
        for entry in inbox.iterdir():
            m = _INBOX_NAME_RE.match(entry.name)
            if m and entry.is_file():
                out[int(m.group(1))] = entry.name
    except OSError:
        pass
    return out


def _annotate(cousin, messages):
    files = _inbox_files(cousin)
    if not files:
        return messages
    for msg in messages:
        name = files.get(msg.get("id"))
        if name:
            msg["attachment"] = {
                "url": "/api/chat/inbound/%s/%s" % (cousin.slug, name)}
    return messages


def guarded(fn):
    """Turn a RouteError raised inside a handler into its (status, body)."""
    def wrapper(req, **kw):
        try:
            return fn(req, **kw)
        except RouteError as err:
            return err.status, err.body
    wrapper.__name__ = fn.__name__
    return wrapper


def register():
    @router.route("GET", "/api/messages")
    @guarded
    def messages(req):
        q = req.query
        _require(q, "cousin", "user")
        cousin = find_cousin(req, q.get("cousin"))
        status, body = _upstream(cousin, "/api/history", query={
            "user": q.get("user"), "since": q.get("since"),
            "before": q.get("before"), "limit": q.get("limit") or "200",
            "archived": q.get("archived") or "0"})
        if status == 200 and isinstance(body, dict):
            _annotate(cousin, body.get("messages") or [])
            body["cousin"] = cousin.slug
        return status, body

    @router.route("GET", "/api/search")
    @guarded
    def search(req):
        q = req.query
        _require(q, "cousin")
        cousin = find_cousin(req, q.get("cousin"))
        status, body = _upstream(cousin, "/api/search", query={
            "q": q.get("q"), "user": q.get("user"),
            "archived": q.get("archived") or "0"})
        if status == 200 and isinstance(body, dict):
            _annotate(cousin, body.get("messages") or [])
            body["cousin"] = cousin.slug
        return status, body

    @router.route("POST", "/api/chat/send")
    @guarded
    def send(req):
        b = req.body
        _require(b, "cousin", "user")
        cousin = find_cousin(req, b.get("cousin"))
        forward = {"user": b.get("user"), "message": b.get("message")}
        for key in ("image", "reply_to"):
            if b.get(key) is not None:
                forward[key] = b[key]
        return _upstream(cousin, "/api/send", body=forward,
                         timeout=_SEND_TIMEOUT)

    @router.route("POST", "/api/chat/archive")
    @guarded
    def archive(req):
        b = req.body
        _require(b, "cousin", "user")
        cousin = find_cousin(req, b.get("cousin"))
        return _upstream(cousin, "/api/archive", body={
            "user": b.get("user"), "keep": b.get("keep", 0)})

    @router.route("POST", "/api/chat/reactions")
    @guarded
    def reactions(req):
        b = req.body
        _require(b, "cousin")
        cousin = find_cousin(req, b.get("cousin"))
        return _upstream(cousin, "/api/reactions", body={
            "message_id": b.get("message_id"), "user": b.get("user"),
            "emoji": b.get("emoji"), "action": b.get("action") or "tap"})

    @router.route("GET", "/api/chat/inbound/{slug}/{name}")
    @guarded
    def inbound(req, slug, name):
        cousin = find_cousin(req, slug)
        name = urllib.parse.unquote(name)
        if not _INBOX_NAME_RE.match(name):
            raise RouteError(404, {"error": "not found"})
        inbox = (cousin.home / "chat" / "inbound").resolve()
        candidate = (inbox / name).resolve()
        if candidate.parent != inbox or not candidate.is_file():
            raise RouteError(404, {"error": "not found"})
        payload = candidate.read_bytes()
        return 200, RawResponse([
            ("Content-Type", _INBOX_TYPES[candidate.suffix.lower()]),
            ("Cache-Control", "private, max-age=3600"),
            ("Content-Length", str(len(payload))),
        ], payload)


register()
