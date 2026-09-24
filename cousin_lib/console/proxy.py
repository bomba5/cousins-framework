"""Chat routes: a proxy over each cousin's chat server
(docs/reference/console-api.md, "Chat: a proxy over each cousin's chat server").

Every route names the cousin, resolves its `host`/`port` from the
filesystem registry on that call, and forwards to the routes in
docs/reference/chat-api.md. A runner cousin (cousin.toml `[agent]
runner`, its home on this machine) runs no chat server: for it the
console answers the same routes itself, over the cousin's `chat.db`,
through the library the chat server answers with (server/chat_api.py),
so the body is the same on both lanes. A send then delivers through
`delivery.deliver` (the cousin's inbox) and a reaction tells the cousin
the same way; the console still keeps no store of its own. The console stores no message: the only files
it touches under a cousin home are the inbox and the generated-media
folders (`chat/images`, `chat/audio`, `chat/video`), served read-only,
and the attachment annotation on history rows is one directory listing
per request plus a check of each row's attachment_path, a projection
and not a store.

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
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request

from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router
from cousin_lib.server import chat_api
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
# Generated media lands under <home>/chat/<folder>/ (docs/media.md,
# "Storage"); the row's attachment_path names the file. Each folder
# holds one display kind, and only these suffixes are served from it.
_MEDIA_FOLDERS = {"images": "image", "audio": "audio", "video": "video"}
_MEDIA_TYPES = {
    "image": dict(_INBOX_TYPES),
    "audio": {".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".oga": "audio/ogg",
              ".opus": "audio/ogg", ".wav": "audio/wav", ".m4a": "audio/mp4",
              ".webm": "audio/webm"},
    "video": {".mp4": "video/mp4", ".webm": "video/webm",
              ".mov": "video/quicktime", ".m4v": "video/mp4"},
}
# The stored kind vocabulary ('image' | 'voice' | 'video') mapped to
# the display one the view renders by.
_DISPLAY_KIND = {"image": "image", "voice": "audio", "audio": "audio",
                 "video": "video"}
_MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
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
    # A remote node the console's queen knows (hive on): its chat
    # server is where it last checked in from.
    from cousin_lib.console import hive as console_hive
    remote, refusal = console_hive.find_remote(
        getattr(req, "server", None), slug)
    if remote is not None:
        return remote
    if refusal is not None:
        raise RouteError(refusal[0], {"ok": False, "error": refusal[1]})
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
    headers = {"Content-Type": "application/json"} if data else {}
    token = getattr(cousin, "auth_token", None)
    if token:
        # A remote node answers a non-loopback caller only with its
        # own hive token; the console, as its queen, holds it.
        headers["Authorization"] = "Bearer %s" % token
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers=headers)
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


def serves_locally(cousin):
    """True for a runner cousin whose home is on this machine: the console
    answers its chat routes over chat.db instead of proxying (a remote
    node has no home here and is always proxied)."""
    home = getattr(cousin, "home", None)
    if home is None:
        return False
    from cousin_lib import delivery
    return isinstance(delivery.backend_for(home), delivery.InboxBackend)


def _local(fn, *args, **kw):
    """(status, body) from a chat_api call: its 200, or the 400 the chat
    server would have answered."""
    try:
        return 200, fn(*args, **kw)
    except chat_api.BadRequest as err:
        return 400, {"error": str(err)}


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


def _media_file(cousin, stored):
    """(folder, name) for a row's attachment_path when it names a
    servable file directly inside one of the media folders, else None."""
    if not stored or not isinstance(stored, str):
        return None
    try:
        candidate = pathlib.Path(stored).resolve()
        chat = (cousin.home / "chat").resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    folder = candidate.parent.name
    kind = _MEDIA_FOLDERS.get(folder)
    if (kind is None or candidate.parent.parent != chat
            or not _MEDIA_NAME_RE.match(candidate.name)
            or candidate.suffix.lower() not in _MEDIA_TYPES[kind]
            or not candidate.is_file()):
        return None
    return folder, candidate.name


def _annotate(cousin, messages):
    """Project each row's attachment as `{"url", "kind"}`: the inbox
    file named by the message id (an image), else the row's
    attachment_path when it sits in a media folder. `kind` is the
    display kind: image, video or audio (a stored 'voice' is audio)."""
    if getattr(cousin, "home", None) is None:
        return messages  # a remote node: no files on this machine
    files = _inbox_files(cousin)
    for msg in messages:
        name = files.get(msg.get("id"))
        if name:
            msg["attachment"] = {
                "url": "/api/chat/inbound/%s/%s" % (cousin.slug, name),
                "kind": "image"}
            continue
        found = _media_file(cousin, msg.get("attachment_path"))
        if found:
            folder, fname = found
            kind = (_DISPLAY_KIND.get(msg.get("attachment_kind") or "")
                    or _MEDIA_FOLDERS[folder])
            msg["attachment"] = {
                "url": "/api/chat/media/%s/%s/%s"
                       % (cousin.slug, folder, urllib.parse.quote(fname)),
                "kind": kind}
    return messages


def _byte_range(header, size):
    """(start, end) inclusive for a single `bytes=` range the file can
    satisfy, None for no usable header, or "unsatisfiable"."""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:
        length = int(m.group(2))
        if length == 0:
            return "unsatisfiable"
        start, end = max(0, size - length), size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        return "unsatisfiable"
    return start, end


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
        query = {"user": q.get("user"), "since": q.get("since"),
                 "before": q.get("before"), "limit": q.get("limit") or "200",
                 "archived": q.get("archived") or "0"}
        if serves_locally(cousin):
            status, body = _local(chat_api.history, cousin.home,
                                  {k: v for k, v in query.items() if v is not None})
        else:
            status, body = _upstream(cousin, "/api/history", query=query)
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
        query = {"q": q.get("q"), "user": q.get("user"),
                 "archived": q.get("archived") or "0"}
        if serves_locally(cousin):
            status, body = _local(chat_api.search, cousin.home,
                                  {k: v for k, v in query.items() if v is not None})
        else:
            status, body = _upstream(cousin, "/api/search", query=query)
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
        if serves_locally(cousin):
            # no recall context: a runner recalls in its own prompt hook
            return _local(chat_api.send, cousin, forward,
                          deliver=chat_api.make_deliver(cousin))
        return _upstream(cousin, "/api/send", body=forward,
                         timeout=_SEND_TIMEOUT)

    @router.route("POST", "/api/chat/archive")
    @guarded
    def archive(req):
        b = req.body
        _require(b, "cousin", "user")
        cousin = find_cousin(req, b.get("cousin"))
        forward = {"user": b.get("user"), "keep": b.get("keep", 0)}
        if serves_locally(cousin):
            return _local(chat_api.archive, cousin.home, forward)
        return _upstream(cousin, "/api/archive", body=forward)

    @router.route("POST", "/api/chat/reactions")
    @guarded
    def reactions(req):
        b = req.body
        _require(b, "cousin")
        cousin = find_cousin(req, b.get("cousin"))
        forward = {"message_id": b.get("message_id"), "user": b.get("user"),
                   "emoji": b.get("emoji"), "action": b.get("action") or "tap"}
        if serves_locally(cousin):
            return _local(chat_api.react, cousin.home, forward,
                          notify=chat_api.make_notify(cousin))
        return _upstream(cousin, "/api/reactions", body=forward)

    @router.route("GET", "/api/chat/inbound/{slug}/{name}")
    @guarded
    def inbound(req, slug, name):
        cousin = find_cousin(req, slug)
        name = urllib.parse.unquote(name)
        if cousin.home is None or not _INBOX_NAME_RE.match(name):
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

    @router.route("GET", "/api/chat/media/{slug}/{folder}/{name}")
    @guarded
    def media(req, slug, folder, name):
        cousin = find_cousin(req, slug)
        name = urllib.parse.unquote(name)
        kind = _MEDIA_FOLDERS.get(folder)
        if (cousin.home is None or kind is None
                or not _MEDIA_NAME_RE.match(name)):
            raise RouteError(404, {"error": "not found"})
        base = (cousin.home / "chat" / folder).resolve()
        candidate = (base / name).resolve()
        ctype = _MEDIA_TYPES[kind].get(candidate.suffix.lower())
        if (ctype is None or candidate.parent != base
                or not candidate.is_file()):
            raise RouteError(404, {"error": "not found"})
        size = candidate.stat().st_size
        headers = getattr(req, "headers", None)
        wanted = _byte_range(headers.get("Range") if headers else None, size)
        if wanted == "unsatisfiable":
            return 416, RawResponse([
                ("Content-Range", "bytes */%d" % size),
                ("Content-Length", "0")], b"")
        common = [("Content-Type", ctype),
                  ("Cache-Control", "private, max-age=3600"),
                  ("Accept-Ranges", "bytes")]
        if wanted is None:
            payload = candidate.read_bytes()
            return 200, RawResponse(
                common + [("Content-Length", str(len(payload)))], payload)
        start, end = wanted
        with candidate.open("rb") as fh:
            fh.seek(start)
            payload = fh.read(end - start + 1)
        return 206, RawResponse(common + [
            ("Content-Range", "bytes %d-%d/%d" % (start, end, size)),
            ("Content-Length", str(len(payload)))], payload)


register()
