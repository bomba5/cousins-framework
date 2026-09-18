"""Shared fixtures for the console tests: a temp framework root, a fake
tmux whose exit code is scriptable per subcommand, a loopback fake chat
server, and an HTTP client with a cookie jar."""
import http.cookiejar
import http.server
import json
import os
import pathlib
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

FAKE_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
sub=""
for a in "$@"; do
  case "$a" in
    has-session|kill-session|capture-pane|new-session|send-keys|resize-window|display-message)
      sub="$a";;
  esac
done
if [ "$sub" = capture-pane ]; then cat "$FAKE_TMUX_PANE" 2>/dev/null; fi
# display-message answers the pane pid probe (#{pane_pid}) with
# FAKE_TMUX_PANE_PID; empty means "tmux printed nothing usable".
if [ "$sub" = display-message ]; then printf '%s\\n' "${FAKE_TMUX_PANE_PID:-}"; fi
var="FAKE_TMUX_RC_$(printf '%s' "$sub" | tr 'a-z-' 'A-Z_')"
rc="${!var:-${FAKE_TMUX_RC:-0}}"
exit "$rc"
"""


class FakeChatServer:
    """Answers /health, /api/history, /api/search, /api/send, /api/archive
    and /api/reactions the way docs/chat-server-spec.md says, from
    scripted rows. Records every send."""

    def __init__(self, slug, messages=None):
        self.slug = slug
        self.messages = list(messages or [])
        self.sends = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/health":
                    self._json(200, {"status": "ok", "slug": outer.slug})
                elif parsed.path == "/api/history":
                    if not q.get("user"):
                        self._json(400, {"error": "user is required"})
                        return
                    limit = int((q.get("limit") or ["200"])[0])
                    rows = outer.messages[-limit:]
                    self._json(200, {"messages": rows, "total": len(rows),
                                     "has_more": False})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/send":
                    outer.sends.append(payload)
                    self._json(200, {"ok": True, "id": len(outer.sends),
                                     "timestamp": "2026-01-01T00:00:00+00:00"})
                else:
                    self._json(404, {"error": "not found"})

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ConsoleCase(unittest.TestCase):
    """A temp root with FRAMEWORK_ROOT set, a fake tmux on a scriptable
    exit code, and a console server started on demand."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.tmux = self.root / "tmux"
        self.tmux.write_text(FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.tmux_log = self.root / "tmux.log"
        self.pane = self.root / "pane.txt"
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "FAKE_TMUX_LOG": str(self.tmux_log),
            "FAKE_TMUX_PANE": str(self.pane),
            "FAKE_TMUX_RC_HAS_SESSION": "1",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        # A port nothing on this machine listens on: a fixed number can
        # be a real cousin's live server on the developer's box.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.dead_port = probe.getsockname()[1]

    def cousin(self, slug, *, name=None, port=-1, extra="", role="helper",
               ctype=None, operator=None):
        if port == -1:
            port = self.dead_port
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "memory").mkdir(exist_ok=True)
        text = ('[cousin]\nslug = "%s"\nname = "%s"\nrole = "%s"\n'
                % (slug, name or slug.capitalize(), role))
        if ctype:
            text += 'type = "%s"\n' % ctype
        text += "\n[chat]\n"
        if port is not None:
            text += "port = %d\n" % port
        if operator:
            text += '\n[operator]\nname = "%s"\n' % operator
        text += extra
        (home / "cousin.toml").write_text(text)
        (home / "CLAUDE.md").write_text("# %s\n" % slug)
        return home

    def tmux_running(self, running=True):
        os.environ["FAKE_TMUX_RC_HAS_SESSION"] = "0" if running else "1"

    def serve(self, *, guard=None, **kw):
        from cousin_lib.console.app import ConsoleServer
        server = ConsoleServer(self.root, guard=guard,
                               tmux_bin=str(self.tmux), **kw)
        server.start()
        self.addCleanup(server.stop)
        self.server = server
        return server

    def fake_chat(self, slug, messages=None):
        fake = FakeChatServer(slug, messages).start()
        self.addCleanup(fake.stop)
        return fake

    # -- HTTP ------------------------------------------------------------

    def request(self, method, path, payload=None, raw=False):
        url = "http://127.0.0.1:%d%s" % (self.server.port, path)
        data = None
        headers = {}
        if payload is not None:
            if isinstance(payload, (bytes, str)):
                data = payload.encode() if isinstance(payload, str) \
                    else payload
            else:
                data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            with self.opener.open(req, timeout=10) as resp:
                status, hdrs, body = resp.status, dict(resp.headers), \
                    resp.read()
        except urllib.error.HTTPError as err:
            status, hdrs, body = err.code, dict(err.headers), err.read()
        if raw:
            return status, hdrs, body
        try:
            return status, json.loads(body)
        except ValueError:
            return status, body

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, payload=None, **kw):
        return self.request("POST", path, payload if payload is not None
                            else {}, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)
