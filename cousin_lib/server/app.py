"""The cousin chat server: one HTTP daemon per cousin.

The web UI, peer cousins, and CLIs are all just HTTP clients of this one
surface. The server owns the message history and delivers inbound
messages into the cousin's terminal session.

Two collaborators are injected seams: `deliver` (terminal delivery;
the tmux injector in production) and `guard` (the network allowlist).
`None` for either means the behavior is absent - a server with no guard
allows everything, which only test harnesses should do.
"""
import argparse
import json
import os
import shutil
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cousin_lib import chat_hooks, delivery, memory_search
from cousin_lib.config import (CousinConfig, FrameworkConfig,
                               MissingConfigError)
from cousin_lib.server.inbound import after_inbound_stored, divert_login_code
from cousin_lib.server.netguard import NetGuard
from cousin_lib.server.storage import (ChatStore, is_operator,
                                       normalize_chat_user, save_data_uri)


class _BadRequest(Exception):
    """Client error carrying the message that becomes the 400 body."""


class StartupError(Exception):
    """The server refuses to start. Missing configuration, an unbindable
    port, or an absent tmux binary are startup errors, not per-message
    log lines."""


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
    path = save_data_uri(home, data_uri, folder="inbound",
                         name=str(message_id))
    if path is None:
        return "[image attached, decode failed]"
    return "[image attached -> Read %s]" % path


# Proactive recall: a colleague remembers without being asked. An
# operator message long enough to carry meaning is searched against the
# cousin's own memory and the best hits ride along on the DELIVERED line
# as one suffix. Names and paths only, never file contents; the stored
# message is untouched (the history is what the operator said, not what
# the cousin was reminded of); any failure means the line delivers bare.
# The gates and the line live in memory_search.recall_context, which the
# SDK runner's prompt hook shares.
def _recall_line(config, message):
    """The '[fw-recall] ...' suffix for an operator message, or None.
    The caller (_recall_context) treats any exception as "no line"."""
    return memory_search.recall_context(config.home, message, config=config)


RECALL_BUDGET_SECONDS = 4.0


def _recall_context(config, user, message):
    """The recall line for an operator's message, or "". It rides the
    delivered item as context and never reaches the stored message.
    Best-effort by contract - a failing search never costs the
    delivery."""
    if not is_operator(config, user):
        return ""
    # Bounded: the search refreshes its index first, and after a big
    # change that can take longer than the console waits for a send.
    # Past the budget the message goes out without the line; the search
    # keeps running on its own thread and leaves the index warm.
    box = {}

    def _run():
        try:
            box["line"] = _recall_line(config, message)
        except Exception as err:  # noqa: BLE001 - never fails the send
            box["error"] = err

    worker = threading.Thread(target=_run, name="recall", daemon=True)
    worker.start()
    worker.join(RECALL_BUDGET_SECONDS)
    if worker.is_alive():
        print("recall: skipped: over the %ss budget"
              % RECALL_BUDGET_SECONDS, file=sys.stderr)
        return ""
    if "error" in box:
        print("recall: skipped: %s" % box["error"], file=sys.stderr)
        return ""
    return box.get("line") or ""


# Chat-pattern hooks: <home>/chat-hooks.json reacts to a message after
# it is stored and delivered. An inject: handler rides the same deliver
# seam as the message, as its own line under a fixed author, with the
# triggering message's id. Best-effort by contract: nothing here may
# turn into a failed send.
def _fire_hooks(server, user, message, message_id):
    chat_hooks.on_message(server.config.home, user=user, message=message,
                          message_id=message_id, slug=server.config.slug,
                          deliver=server.deliver)


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


def build_server(home, *, framework_root=None, tmux_bin=None,
                 terminal_delivery=True):
    """Assemble a ChatServer with its real seams: the netguard from the
    install's allowlist config, and tmux delivery unless disabled. All
    startup problems surface here, before the socket accepts anything."""
    try:
        config = CousinConfig.load(home)
        config.require_chat_port()
    except MissingConfigError as err:
        raise StartupError(str(err))
    root = (framework_root or os.environ.get("FRAMEWORK_ROOT")
            or FrameworkConfig.root_from_home(config.home))
    guard = NetGuard.from_config(Path(root)) if root else NetGuard()
    deliver = notify = None
    if terminal_delivery:
        tmux_bin = tmux_bin or shutil.which("tmux")
        if not tmux_bin or not os.access(tmux_bin, os.X_OK):
            raise StartupError(
                "tmux binary not found; terminal delivery is enabled and "
                "cannot work without it"
            )
        opts = dict(tmux_bin=tmux_bin,
                    socket=os.environ.get("COUSIN_TMUX_SOCKET"),
                    root=Path(root) if root else None)

        def deliver(*, user, message, message_id, attachments=(),
                    context=""):
            # wait=False: the line is composed here, in the request
            # thread, because its time prefix reads the presence marker
            # before after_inbound_stored touches it; only the typing
            # happens on a background thread.
            source = "hook" if user == chat_hooks.HOOK_SENDER else "chat"
            thread = (delivery.thread_id("system") if source == "hook"
                      else delivery.thread_for_chat(config, user))
            item = delivery.Item(
                thread_id=thread, source=source, sender=user, body=message,
                attachments=tuple(attachments), context=context,
                message_id=message_id)
            return delivery.deliver(config.home, item, wait=False, **opts)

        def notify(text):
            item = delivery.Item(thread_id=delivery.thread_id("system"),
                                 source="reaction", body=text)
            return delivery.deliver(config.home, item, wait=False, **opts)
    try:
        return ChatServer(config, deliver=deliver, guard=guard,
                          notify=notify)
    except OSError as err:
        raise StartupError("cannot bind chat port: %s" % err)


def serve_main(argv=None):
    """Console entry point: cousin-chat-server --home <cousin home>."""
    parser = argparse.ArgumentParser(prog="cousin-chat-server")
    parser.add_argument("--home", required=True)
    parser.add_argument("--no-terminal-delivery", action="store_true")
    args = parser.parse_args(argv)
    try:
        server = build_server(
            args.home,
            terminal_delivery=not args.no_terminal_delivery,
        )
    except StartupError as err:
        print("cousin-chat-server: %s" % err, file=sys.stderr)
        return 2
    print("cousin-chat-server: %s on port %d"
          % (server.config.slug, server.port))
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


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
        # R18: a login code is stored redacted and delivered to nobody;
        # no recall, no marker, no hook ever sees it.
        diverted = divert_login_code(self.chat_server.config, user, message)
        if diverted is not None:
            row = self._with_store(lambda store: store.add_message(
                chat_user=normalize_chat_user(user), user=user, message=diverted,
                msg_type="user"))
            self._send_json(200, {"ok": True, "id": row["id"], "timestamp": row["timestamp"],
                                  "diverted": True})
            return
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
            # The recall line rides as context, in the DELIVERED item
            # only: the row above already holds the message as the
            # operator wrote it. Fire-and-forget by design: the outcome
            # (delivered/queued/failed) is not read here.
            server.deliver(user=user, message=message,
                           message_id=row["id"], attachments=attachments,
                           context=_recall_context(server.config, user,
                                                   message))
        # After delivery composed its text: the marker's mtime is the
        # gap baseline for the NEXT message, not this one, and the
        # correction capture rides along on the same call.
        after_inbound_stored(server.config, user, message)
        # Hooks last: the message is stored and its delivery composed,
        # so a hook's inject line is unambiguously the second line.
        _fire_hooks(server, user, message, row["id"])
        self._send_json(200, {
            "ok": True, "id": row["id"], "timestamp": row["timestamp"],
        })

    def _handle_reply(self):
        # The cousin's own outbound: stored under the recipient's thread,
        # never delivered back into its own pane. The slug-bound path that
        # routed here already rejected misroutes with a 404.
        body = self._read_json()
        message = body.get("message") or ""
        reply_to_user = body.get("reply_to_user")
        attachment = body.get("attachment") or {}
        kind = attachment.get("kind")
        path = attachment.get("path")
        # A caption-less attachment is a valid reply: message OR
        # attachment, not message required.
        if not message and not (kind and path):
            raise _BadRequest(
                "a reply needs a non-empty message or an attachment")
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
            attachment_kind=kind,
            attachment_path=path,
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


if __name__ == "__main__":
    sys.exit(serve_main())
