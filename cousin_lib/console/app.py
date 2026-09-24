"""The console server: a threaded http.server behind the network guard
that owns two things, browser sessions and the users file, and serves
everything else as a projection of a store some other component owns
(docs/reference/console-api.md, docs/reference/console-api.md).

Extension points for the other console tasks:

- `ROUTE_MODULES`: the list of module names whose import registers
  routes on `router`. Every name is imported when a `ConsoleServer` is
  constructed and its `register()` called again, so a registry cleared
  by a test, or a module appended to the list after import, still
  serves. Route modules keep per-server state on `req.server.state`,
  never at module level.
- `ConsoleServer.static_handler`: `None` means `static.serve_static`
  over `ConsoleServer.static_dir` (suffix allowlist, traversal check,
  no-store, index for `/`); a frontend task may assign
  `fn(handler, path)` instead.
- `ConsoleServer.emit(kind, data)`: command handlers announce what
  they did (`cousin-status`, `cousin-flip`, `tracker-change`, the job
  events); it fans out to `sse.emit` (every open `/api/events` stream)
  and to `ConsoleServer.listeners` (tests).
- The request a handler reads: `req.user` (the session user, None when
  auth is not configured), `req.server` (the `ConsoleServer`: `root`,
  `tmux_bin`, `tmux_socket`, `state`, `users`, `sessions`), and the
  duck-type the chat, pane and events modules read: `req.root`,
  `req.query` (first value per key), `req.body` (the parsed JSON
  object; malformed JSON is this module's 400), `req.tmux_bin`,
  `req.tmux_socket`.
- A handler returns `(status, json_body)` or `(status, json_body,
  headers)`; a `static.RawResponse` body is written verbatim and an
  `sse.Stream` body is streamed chunk by chunk and closed.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cousin_lib.config import FrameworkConfig, MissingConfigError
from cousin_lib.console import router
from cousin_lib.console.sse import Stream
from cousin_lib.console.static import STATIC_DIR, RawResponse, serve_static

ROUTE_MODULES = [
    "cousin_lib.console.auth",
    "cousin_lib.console.routes_fleet",
    "cousin_lib.console.routes_jobs",
    "cousin_lib.console.routes_loops",
    "cousin_lib.console.routes_memory",
    "cousin_lib.console.routes_files",
    "cousin_lib.console.routes_shared",
    "cousin_lib.console.routes_admin",
    "cousin_lib.console.routes_tracker",
    "cousin_lib.console.routes_prefs",
    "cousin_lib.console.routes_meetings",
    "cousin_lib.console.routes_telegram",
    "cousin_lib.console.hive",
    "cousin_lib.console.proxy",
    "cousin_lib.console.pane",
    "cousin_lib.console.stream",
    "cousin_lib.console.sse",
]

# /api/version is public: the login page may show it, and it says only
# which release (and commit) this console runs.
AUTH_EXEMPT = {("POST", "/api/auth/login"), ("GET", "/api/auth/me"),
               ("GET", "/api/version")}

DEFAULT_STATIC_DIR = STATIC_DIR


class HttpError(Exception):
    """A handler's refusal: status plus the error body."""

    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status = status
        self.body = {"ok": False, "error": error}
        self.body.update(extra)


class MalformedJSON(Exception):
    """The request body is not a JSON object."""


class Request:
    def __init__(self, handler, server, method, path, query, raw_body):
        self.handler = handler
        self.server = server
        self.root = server.root
        self.tmux_bin = server.tmux_bin
        self.tmux_socket = server.tmux_socket
        self.method = method
        self.path = path
        self.query_all = query
        self.query = {k: v[0] for k, v in query.items() if v}
        self.headers = handler.headers
        self.client = handler.client_address[0]
        self.raw_body = raw_body
        self.body = {}
        self.user = None
        self.session_token = None

    def parse_body(self):
        """The body as a dict; {} for an empty body; MalformedJSON for
        anything that does not parse to an object."""
        if not self.raw_body.strip():
            self.body = {}
            return self.body
        try:
            data = json.loads(self.raw_body)
        except ValueError:
            raise MalformedJSON()
        if not isinstance(data, dict):
            raise MalformedJSON()
        self.body = data
        return data

    def json(self):
        return self.body

    def int_query(self, name, default=None):
        raw = self.query.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise HttpError(400, "%s must be an integer" % name)


def load_routes():
    """Import every route module and call its `register()` so the
    registrations are on the registry even after a `router.clear()`.
    Registration is idempotent (the router replaces a repeated
    method+pattern), so calling it again costs nothing."""
    for name in ROUTE_MODULES:
        module = importlib.import_module(name)
        register = getattr(module, "register", None)
        if callable(register):
            register()


def default_static(handler, path):
    """`static.serve_static` over the server's static directory."""
    status, headers, body = serve_static(path, root=handler.console.static_dir)
    handler.send_response(status)
    for key, value in headers:
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


class ConsoleServer:
    def __init__(self, root, *, guard=None, host="127.0.0.1", port=0,
                 tmux_bin="tmux", tmux_socket=None, users_path=None,
                 secure_cookie=False):
        from cousin_lib.console import auth

        self.root = Path(root)
        self.guard = guard
        self.tmux_bin = tmux_bin
        self.tmux_socket = tmux_socket
        self.secure_cookie = secure_cookie
        self.users = auth.Users(
            Path(users_path) if users_path
            else self.root / "config" / "console-users.json")
        self.sessions = auth.Sessions(
            path=self.root / "data" / "console-sessions.json")
        self._last_users_error = None
        self.started_at = time.time()
        # Read the version and commit now, once: the top bar shows what
        # this process RUNS, so a bumped or pulled checkout that was not
        # restarted is visible as the old values.
        from cousin_lib import version as _version
        _version.version()
        _version.git_commit()
        self.listeners = []
        self.state = {}
        self.static_dir = DEFAULT_STATIC_DIR
        self.static_handler = None
        self.settle_seconds = 1.0
        self.flip_fn = None
        self.close_fn = None
        self.exit_fn = None
        # Every library the console calls reads FRAMEWORK_ROOT; the
        # entry point exports the flag, a direct construction inherits
        # the environment and fills it only when it is empty.
        os.environ.setdefault("FRAMEWORK_ROOT", str(self.root))
        load_routes()
        console = self

        class Handler(_Handler):
            pass

        Handler.console = console
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self._thread = None

    @property
    def port(self):
        return self.httpd.server_address[1]

    def emit(self, kind, data):
        """Announce a command's effect: to every open events stream and
        to the in-process listeners."""
        from cousin_lib.console import sse
        try:
            sse.emit(kind, data)
        except Exception:
            pass
        for listener in list(self.listeners):
            try:
                listener(kind, data)
            except Exception:
                continue

    def _wire_events(self):
        """The events stream's snapshot and refresh rows are the same
        rows the GET views return: plug the fleet and loops enrichers
        in and start the store poller for this root."""
        from cousin_lib.console import routes_fleet, routes_loops, sse
        sse.configure(
            cousins=lambda: routes_fleet.fleet_rows(self),
            loops=lambda: routes_loops.loops_rows(self)["loops"])
        sse.stop_poller()
        sse.start_poller(root=self.root)

    def report_users_error(self, error):
        """Log a broken users file to stderr (the unit's journal) once
        per distinct error, not once per request."""
        if error != self._last_users_error:
            self._last_users_error = error
            print("cousin-console: %s" % error, file=sys.stderr,
                  flush=True)

    def serve_forever(self):
        """The foreground entry the CLI uses: wire the event sources,
        then serve. The threaded start() below wires too; the two must
        never drift, because the events stream served without the
        wiring carries bare registry rows (no status), which the
        sidebar renders as every cousin stopped."""
        self._wire_events()
        self.httpd.serve_forever()

    def start(self):
        self._wire_events()
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._thread.start()

    def stop(self):
        from cousin_lib.console import hive as console_hive
        from cousin_lib.console import sse
        sse.stop_poller()
        console_hive.cleanup(self)
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                # The socket is closed, so no port is held, but the thread
                # outlives this call and whatever runs next. Silence here
                # used to make that invisible.
                print("cousin-console: the serving thread is still running"
                      " 5s after shutdown; it outlives this stop",
                      file=sys.stderr)


class _Handler(BaseHTTPRequestHandler):
    console: ConsoleServer = None

    def log_message(self, fmt, *args):
        pass

    # -- writers ---------------------------------------------------------

    def send_bytes(self, status, payload, headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, default=str).encode()
        merged = {"Content-Type": "application/json",
                  "Cache-Control": "no-store"}
        merged.update(headers or {})
        self.send_bytes(status, body, merged)

    # -- dispatch --------------------------------------------------------

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _handle(self, method):
        server = self.console
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        # The hive's door (cousin_lib/console/hive.py): nodes have no
        # session and may sit on any network, so /hive/ authenticates
        # with the hive token alone, ahead of the guard and the login.
        # With the hive off it is a 404 like any unknown path. Its body
        # is bounded BEFORE it is read: nothing in front of this door
        # has vetted the caller yet.
        from cousin_lib.console import hive as console_hive
        if console_hive.is_hive_path(parsed.path):
            if length < 0 or length > console_hive.MAX_BODY_BYTES:
                self.close_connection = True
                self.send_json(413, {"error": "body too large"})
                return
            body = self.rfile.read(length) if length else b""
            console_hive.serve(self, method, parsed.path, parsed.query, body)
            return
        # The external peers' door (cousin_lib/console/peer_routes.py):
        # another install's cousin, with its per-peer bearer token and no
        # session, behind the network guard; the body is bounded before
        # it is read. A session is worth nothing there, a peer token
        # nothing under /api/.
        from cousin_lib.console import peer_routes
        if peer_routes.is_peer_path(parsed.path):
            if server.guard is not None and not server.guard(self.client_address[0]):
                self.send_json(403, {"error": "address not allowed"})
                return
            if length < 0 or length > peer_routes.MAX_BODY_BYTES:
                self.close_connection = True
                self.send_json(413, {"error": "body too large"})
                return
            body = self.rfile.read(length) if length else b""
            peer_routes.serve(self, method, parsed.path, body)
            return
        length = max(0, length)
        body = self.rfile.read(length) if length else b""
        if server.guard is not None and not server.guard(
                self.client_address[0]):
            self.send_json(403, {"error": "address not allowed"})
            return
        if not (parsed.path == "/api" or parsed.path.startswith("/api/")):
            if method != "GET":
                self.send_json(404, {"error": "not found"})
                return
            (server.static_handler or default_static)(self, parsed.path)
            return
        req = Request(self, server, method, parsed.path,
                      urllib.parse.parse_qs(parsed.query), body)
        from cousin_lib.console import auth
        token = self._cookie(auth.COOKIE)
        if token:
            req.session_token = token
            req.user = server.sessions.lookup(
                token, stamp_of=lambda name: auth.user_stamp(
                    server.users, name))
        route = (method, parsed.path.rstrip("/") or parsed.path)
        users_state, users_error = server.users.state()
        if users_state == "broken":
            # Fail closed: a users file that is present but unusable
            # never reads as "no users". Only `me` answers (it says
            # why); every other route, login included and any live
            # session regardless, is refused.
            server.report_users_error(users_error)
            if route != ("GET", "/api/auth/me"):
                self.send_json(503, {"ok": False, "error": users_error})
                return
        elif (users_state == "ok" and req.user is None
                and route not in AUTH_EXEMPT):
            self.send_json(401, {"ok": False, "error": "login required"})
            return
        try:
            req.parse_body()
            result = router.dispatch(method, parsed.path, req=req)
        except MalformedJSON:
            result = (400, {"ok": False, "error": "malformed JSON"})
        except HttpError as err:
            result = (err.status, err.body)
        except Exception as err:  # noqa: BLE001 - the wire needs a body
            result = (500, {"ok": False,
                            "error": "%s: %s" % (type(err).__name__, err)})
        self._write(result)

    def _write(self, result):
        if len(result) == 3:
            status, payload, headers = result
        else:
            status, payload = result
            headers = None
        if isinstance(payload, RawResponse):
            self.send_response(status)
            for key, value in payload.headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload.body)
            return
        if isinstance(payload, Stream):
            self.send_response(status)
            self.send_header("Content-Type", payload.content_type)
            for key, value in payload.headers:
                self.send_header(key, value)
            self.end_headers()
            try:
                for chunk in payload:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                payload.close()
                # A stream's keep-alive header must not leave the socket
                # waiting for a second request once the stream is over.
                self.close_connection = True
            return
        self.send_json(status, payload, headers)


# -- entry point -------------------------------------------------------------

def _parser():
    parser = argparse.ArgumentParser(
        prog="cousin-console",
        description="the web console: serve, or adduser <name>")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=("serve", "adduser"))
    parser.add_argument("name", nargs="?",
                        help="adduser: the user to create or reset")
    parser.add_argument(
        "--root",
        help="the framework root: a directory containing cousins/ and"
             " config/ (typically the checkout). Falls back to"
             " FRAMEWORK_ROOT.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--tmux-bin", default="tmux")
    parser.add_argument("--tmux-socket")
    parser.add_argument("--secure-cookie", action="store_true",
                        help="mark the session cookie Secure (behind TLS)")
    return parser


def _resolve_root(flag):
    root = FrameworkConfig.resolve(flag, cwd_fallback=True).root
    # Every library the console calls reads FRAMEWORK_ROOT; the flag is
    # the same fact told once, so it wins here and is exported.
    os.environ["FRAMEWORK_ROOT"] = str(root)
    return root


def build_console_from_cli(argv=None):
    from cousin_lib.server.netguard import NetGuard

    args = _parser().parse_args(argv)
    root = _resolve_root(args.root)
    guard = NetGuard.from_config(root)
    return ConsoleServer(root, guard=guard, host=args.host, port=args.port,
                         tmux_bin=args.tmux_bin, tmux_socket=args.tmux_socket,
                         secure_cookie=args.secure_cookie)


def auth_banner(users):
    """The startup line's auth suffix: empty when enforced, the first-run
    hint when absent, a CLOSED warning when the file is broken."""
    state, _ = users.state()
    if state == "ok":
        return ""
    if state == "missing":
        return " (auth not configured: cousin-console adduser <name>)"
    return (" (CLOSED: %s is present but unusable; every /api route"
            " answers 503 until it is fixed)" % users.path)


def _adduser(root, name):
    import getpass

    from cousin_lib.console import auth

    if not name:
        print("cousin-console: adduser needs a name", file=sys.stderr)
        return 2
    users = auth.Users(root / "config" / "console-users.json")
    state, error = users.state()
    if state == "broken":
        # Never paper over a corrupt file with a fresh one: it may be
        # the only copy of every other user's hash.
        print("cousin-console: %s; nothing written" % error,
              file=sys.stderr)
        return 1
    first = getpass.getpass("password for %s: " % name)
    second = getpass.getpass("again: ")
    if first != second:
        print("cousin-console: passwords differ; nothing written",
              file=sys.stderr)
        return 2
    if len(first) < auth.MIN_PASSWORD_CHARS:
        print("cousin-console: password shorter than %d characters;"
              " nothing written" % auth.MIN_PASSWORD_CHARS, file=sys.stderr)
        return 2
    try:
        users.set_password(name, first)
    except auth.UsersFileError as err:
        print("cousin-console: %s; nothing written" % err, file=sys.stderr)
        return 1
    print("cousin-console: user %s set in %s" % (name, users.path))
    return 0


def console_main(argv=None):
    """cousin-console [--root R] [--port N] [--host H] [--tmux-bin B]
    [--tmux-socket S] | cousin-console adduser <name>."""
    args = _parser().parse_args(argv)
    try:
        root = _resolve_root(args.root)
    except MissingConfigError as err:
        print("cousin-console: %s" % err, file=sys.stderr)
        return 2
    if args.command == "adduser":
        return _adduser(root, args.name)
    from cousin_lib.server.netguard import NetGuard

    server = ConsoleServer(root, guard=NetGuard.from_config(root),
                           host=args.host, port=args.port,
                           tmux_bin=args.tmux_bin,
                           tmux_socket=args.tmux_socket,
                           secure_cookie=args.secure_cookie)
    print("cousin-console: serving %s on %s:%d%s"
          % (server.root, server.httpd.server_address[0], server.port,
             auth_banner(server.users)),
          flush=True)  # a unit's stdout is a pipe: flush or never seen
    state, error = server.users.state()
    if state == "broken":
        server.report_users_error(error)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - the -m launcher
    raise SystemExit(console_main())
