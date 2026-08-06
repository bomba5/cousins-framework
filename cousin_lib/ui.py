"""The web UI daemon: a projection over stores the framework owns.

The governing rule, from docs/ui-spec.md: a UI process that dies loses
nothing but its pixels. This daemon holds no state - every view is
read from the store that owns it on each request, and every command
writes through the same library a CLI uses. A restart costs a refresh,
never data; a UI that never started costs an install nothing but a
page.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cousin_lib.config import FrameworkConfig

_STATIC_DIR = Path(__file__).parent / "ui_static"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class UIServer:
    def __init__(self, root, *, guard=None, host="127.0.0.1", port=0):
        self.root = Path(root)
        self.guard = guard
        server = self

        class Handler(_UIHandler):
            ui = server

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self._thread = None

    @property
    def port(self):
        return self.httpd.server_address[1]

    def start(self):
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)


def build_ui(root, *, guard=None, host="127.0.0.1", port=0):
    return UIServer(root, guard=guard, host=host, port=port)


def ui_main(argv=None):
    """Console entry point: cousin-ui [--port N] [--host H]. The guard
    is the network allowlist, exactly as for the chat server - the
    only boundary, stated as address trust, not user identity."""
    import argparse
    import sys

    from cousin_lib.server.netguard import NetGuard

    parser = argparse.ArgumentParser(prog="cousin-ui")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    args = parser.parse_args(argv)
    try:
        root = FrameworkConfig.from_env().root
    except Exception as err:
        print("cousin-ui: %s" % err, file=sys.stderr)
        return 2
    guard = NetGuard.from_config(root)
    server = build_ui(root, guard=guard, host=args.host, port=args.port)
    print("cousin-ui: serving %s on %s:%d"
          % (root, args.host, server.port))
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


class _UIHandler(BaseHTTPRequestHandler):
    ui = None

    def log_message(self, fmt, *args):
        pass

    def _guard_denies(self):
        guard = self.ui.guard
        if guard is not None and not guard(self.client_address[0]):
            self._json(403, {"error": "address not allowed"})
            return True
        return False

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _framework(self):
        return FrameworkConfig(self.ui.root)

    def do_GET(self):
        if self._guard_denies():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            cousins = self._framework().list_cousins()
            self._json(200, {"status": "ok", "root": str(self.ui.root),
                             "cousins": len(cousins)})
        elif path == "/api/cousins":
            self._view_cousins()
        elif path == "/api/jobs":
            self._view_jobs()
        elif path == "/api/loops":
            self._view_loops()
        elif path == "/api/loops/requests":
            self._view_requests()
        else:
            self._serve_static(path)

    def do_POST(self):
        if self._guard_denies():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/loops/fire":
            self._cmd_fire()
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        # Guarded exactly like every other route - the source left this
        # one ungated, which a rewrite must not reproduce.
        if self._guard_denies():
            return
        self._json(501, {"error": "not implemented"})

    def _view_cousins(self):
        # Read from the filesystem registry every call: the view cannot
        # go stale against a cousin created a moment ago because the
        # daemon keeps no list of its own.
        rows = []
        for config in self._framework().list_cousins():
            rows.append({
                "slug": config.slug,
                "name": config.name,
                "chat_port": config.chat_port,
                "type": config.type,
            })
        self._json(200, {"cousins": rows})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return None

    def _view_jobs(self):
        from cousin_lib import jobs
        self._json(200, {"jobs": jobs.list_jobs()})

    def _view_loops(self):
        from cousin_lib import loops
        self._json(200, {"daemon": loops.daemon_status()})

    def _view_requests(self):
        from cousin_lib import loops
        self._json(200, {"requests": loops.list_requests()})

    def _cmd_fire(self):
        from cousin_lib import loops
        body = self._read_body()
        if body is None:
            self._json(400, {"error": "malformed JSON"})
            return
        cousin = body.get("cousin")
        loop = body.get("loop")
        if not cousin or not loop:
            self._json(400, {"error": "cousin and loop are required"})
            return
        # A command writes through the loops daemon's request store -
        # the same store a CLI writes; the UI holds nothing.
        request_id = loops.submit_request(
            "fire", cousin=cousin, payload={"loop": loop})
        self._json(200, {"ok": True, "request_id": request_id})

    def _serve_static(self, path):
        name = path.lstrip("/") or "index.html"
        candidate = (_STATIC_DIR / name).resolve()
        root = _STATIC_DIR.resolve()
        if (not candidate.is_relative_to(root)
                or candidate.suffix.lower() not in _STATIC_TYPES
                or not candidate.is_file()):
            self._json(404, {"error": "not found"})
            return
        payload = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         _STATIC_TYPES[candidate.suffix.lower()])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
