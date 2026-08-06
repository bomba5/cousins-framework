"""The cousin chat server: one HTTP daemon per cousin.

The web UI, peer cousins, and CLIs are all just HTTP clients of this one
surface. The server owns the message history and delivers inbound
messages into the cousin's terminal session.

Two collaborators are injected seams: `deliver` (terminal delivery;
the tmux injector in production) and `guard` (the network allowlist).
`None` for either means the behavior is absent - a server with no guard
allows everything, which only test harnesses should do.
"""
import base64
import binascii
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cousin_lib.server.storage import ChatStore, normalize_chat_user


class _BadRequest(Exception):
    """Client error carrying the message that becomes the 400 body."""


# Extensions the inbox writes as-is; anything else is normalized to .bin
# so a crafted subtype cannot choose an arbitrary filename suffix.
_INBOX_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# What <home>/www may serve, with the content type each maps to. An
# allowlist rather than a denylist: a file type nobody thought about is a
# file type that does not get served.
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def persist_inbound_file(home, message_id, data_uri):
    """Decode an inbound data: image to <home>/chat/inbound/<id>.<ext> and
    return the delivery marker for it. The terminal line cannot carry
    megabytes of base64; the file is the handoff."""
    m = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64,(.*)$",
                 data_uri, re.S)
    if m:
        try:
            payload = base64.b64decode(m.group(2), validate=True)
        except (ValueError, binascii.Error):
            payload = None
    else:
        payload = None
    if payload is None:
        return "[image attached, decode failed]"
    ext = m.group(1).lower()
    if ext not in _INBOX_EXTENSIONS:
        ext = "bin"
    inbox = home / "chat" / "inbound"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / ("%d.%s" % (message_id, ext))
    path.write_bytes(payload)
    return "[image attached -> Read %s]" % path


class ChatServer:
    def __init__(self, config, *, deliver=None, guard=None, notify=None):
        self.config = config
        self.deliver = deliver
        self.guard = guard
        self.notify = notify
        self.db_path = config.home / "data" / "chat.db"
        host = config.chat_host or "127.0.0.1"
        server = self

        class Handler(_ChatHandler):
            chat_server = server

        self.httpd = ThreadingHTTPServer(
            (host, config.require_chat_port()), Handler
        )
        self._thread = None

    @property
    def port(self):
        return self.httpd.server_address[1]

    def start(self):
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)


class _ChatHandler(BaseHTTPRequestHandler):
    chat_server = None  # bound per ChatServer via subclassing

    def log_message(self, fmt, *args):
        pass  # request logging is the caller's concern, not stderr's

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        """Parse the request body. Malformed JSON is a 400, never silently
        an empty object."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise _BadRequest("malformed JSON body")
        if not isinstance(body, dict):
            raise _BadRequest("JSON body must be an object")
        return body

    def _with_store(self, fn):
        """Run fn(store) with a per-request connection, closed explicitly
        when the request finishes - relying on GC here leaks native
        allocator arenas under large payloads."""
        store = ChatStore(self.chat_server.db_path)
        try:
            return fn(store)
        finally:
            store.close()

    def _guard_denies(self):
        """Run the network guard before anything else. The denial carries
        the CORS header so a browser shows a 403 rather than an opaque
        network error."""
        guard = self.chat_server.guard
        if guard is not None and not guard(self.client_address[0]):
            self._send_json(403, {"error": "address not allowed"})
            return True
        return False

    def do_GET(self):
        if self._guard_denies():
            return
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(200, {
                    "status": "ok",
                    "slug": self.chat_server.config.slug,
                    "port": self.chat_server.port,
                })
            elif parsed.path == "/api/history":
                self._handle_history(urllib.parse.parse_qs(parsed.query))
            elif parsed.path == "/api/search":
                self._handle_search(urllib.parse.parse_qs(parsed.query))
            else:
                self._serve_static(parsed.path)
        except _BadRequest as err:
            self._send_json(400, {"error": str(err)})

    def do_POST(self):
        if self._guard_denies():
            return
        try:
            if self.path == "/api/send":
                self._handle_send()
            elif self.path == "/api/%s_reply" % self.chat_server.config.slug:
                self._handle_reply()
            elif self.path == "/api/reactions":
                self._handle_reactions()
            elif self.path == "/api/archive":
                self._handle_archive()
            else:
                self._send_json(404, {"error": "not found"})
        except _BadRequest as err:
            self._send_json(400, {"error": str(err)})

    def _handle_send(self):
        body = self._read_json()
        user = body.get("user")
        message = body.get("message")
        if not user or not message:
            raise _BadRequest("user and a non-empty message are required")
        reply_to = body.get("reply_to")
        row = self._with_store(lambda store: store.add_message(
            chat_user=normalize_chat_user(user),
            user=user,
            message=message,
            msg_type="user",
            reply_to=json.dumps(reply_to) if reply_to is not None else None,
        ))
        server = self.chat_server
        attachments = []
        image = body.get("image")
        if image:
            attachments.append(persist_inbound_file(
                server.config.home, row["id"], image
            ))
        if server.deliver is not None:
            server.deliver(user=user, message=message,
                           message_id=row["id"], attachments=attachments)
        # Touched after delivery composed its text: the marker's mtime is
        # the gap baseline for the NEXT message, not this one.
        marker = server.config.home / "data" / ".last-user-msg"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        self._send_json(200, {
            "ok": True, "id": row["id"], "timestamp": row["timestamp"],
        })

    def _handle_reply(self):
        # The cousin's own outbound: stored under the recipient's thread,
        # never delivered back into its own pane. The slug-bound path that
        # routed here already rejected misroutes with a 404.
        body = self._read_json()
        message = body.get("message")
        reply_to_user = body.get("reply_to_user")
        if not message:
            raise _BadRequest("a non-empty message is required")
        if not reply_to_user:
            raise _BadRequest(
                "reply_to_user is required: there is no default recipient"
            )
        reply_to = body.get("reply_to")
        config = self.chat_server.config
        row = self._with_store(lambda store: store.add_message(
            chat_user=normalize_chat_user(reply_to_user),
            user=config.name,
            message=message,
            msg_type=config.slug,
            reply_to=json.dumps(reply_to) if reply_to is not None else None,
            reply_to_user=reply_to_user,
        ))
        self._send_json(200, {
            "ok": True, "id": row["id"], "timestamp": row["timestamp"],
        })

    def _handle_history(self, query):
        user = (query.get("user") or [None])[0]
        if not user:
            raise _BadRequest("user is required")

        def _int(name):
            raw = (query.get(name) or [None])[0]
            if raw is None:
                return None
            try:
                return int(raw)
            except ValueError:
                raise _BadRequest("%s must be an integer" % name)

        out = self._with_store(lambda store: store.history(
            user,
            since=_int("since"),
            before=_int("before"),
            limit=_int("limit") or 200,
            archived=(query.get("archived") or ["0"])[0],
        ))
        self._send_json(200, out)

    def _handle_reactions(self):
        body = self._read_json()
        message_id = body.get("message_id")
        user = body.get("user")
        emoji = body.get("emoji")
        action = body.get("action")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise _BadRequest("message_id must be an integer")
        if not user or not emoji:
            raise _BadRequest("user and emoji are required")
        if action not in ("tap", "remove"):
            raise _BadRequest("action must be 'tap' or 'remove'")
        out = self._with_store(lambda store: store.react(
            message_id, user=user, emoji=emoji, action=action
        ))
        server = self.chat_server
        if out["op"] in ("added", "bumped") and server.notify is not None:
            mine = next(r for r in out["reactions"]
                        if r["user"] == user and r["emoji"] == emoji)
            server.notify(
                "[fw-reaction] msg-id=%d emoji=%s user=%s tap_count=%d"
                " op=%s" % (message_id, emoji, user, mine["tap_count"],
                            out["op"])
            )
        self._send_json(200, out)

    def _handle_archive(self):
        body = self._read_json()
        user = body.get("user")
        keep = body.get("keep", 0)
        if not user:
            raise _BadRequest("user is required")
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
            raise _BadRequest("keep must be a non-negative integer")
        archived = self._with_store(
            lambda store: store.archive(user, keep=keep)
        )
        self._send_json(200, {"ok": True, "archived": archived})

    def _serve_static(self, path):
        """Serve <home>/www for the extension allowlist. The resolved path
        must sit strictly inside the root: symlinks and dot-segments both
        resolve before the containment check. A cousin without www/ simply
        has no pages."""
        root = self.chat_server.config.home / "www"
        name = urllib.parse.unquote(path.lstrip("/"))
        candidate = (root / name).resolve() if name else None
        if (
            candidate is None
            or not root.is_dir()
            or not candidate.is_relative_to(root.resolve())
            or candidate.suffix.lower() not in _STATIC_TYPES
            or not candidate.is_file()
        ):
            self._send_json(404, {"error": "not found"})
            return
        payload = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         _STATIC_TYPES[candidate.suffix.lower()])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_search(self, query):
        q = (query.get("q") or [None])[0]
        if not q:
            raise _BadRequest("q is required")
        user = (query.get("user") or [None])[0]
        hits = self._with_store(lambda store: store.search(
            q,
            user=user,
            archived=(query.get("archived") or ["0"])[0],
        ))
        self._send_json(200, {"messages": hits})
