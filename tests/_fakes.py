"""Fakes shared by the tests.

`fake_embedder()` serves the embedding contract the framework declares
(POST {"model", "prompt"} -> {"embedding": [...]}) over real HTTP on a
loopback port, so the semantic leg is exercised end to end with only
the vectors scripted. The default vector is deterministic and
text-dependent, which is what an incremental index needs: the same
text always embeds the same, different text usually differs.
"""
import contextlib
import http.server
import json
import os
import pathlib
import sqlite3
import stat
import threading
from unittest import mock


def default_vector(text):
    return [float(len(text) % 7), 1.0, 0.5]


@contextlib.contextmanager
def sqlite_left_open():
    """Track every sqlite3 connection opened inside the block. The list
    it yields holds, once the block ends, the database of each one never
    closed (those are then closed here). This works on every Python:
    sqlite3 warns about an unclosed connection only from 3.13."""
    real = sqlite3.connect
    opened, left = [], []

    class Tracked(sqlite3.Connection):
        was_closed = False

        def close(self):
            self.was_closed = True
            super().close()

    def connect(database, *args, **kwargs):
        kwargs.setdefault("factory", Tracked)
        conn = real(database, *args, **kwargs)
        opened.append((str(database), conn))
        return conn

    try:
        with mock.patch("sqlite3.connect", connect):
            yield left
    finally:
        for database, conn in opened:
            if not conn.was_closed:
                left.append(database)
                conn.close()


@contextlib.contextmanager
def fake_embedder(vector_for=None, calls=None):
    """Serve a fake embedding endpoint; yields its URL.

    vector_for: optional callable text -> list[float] (default:
    default_vector); returning None makes the server answer 503 for
    that prompt, which is how a test scripts a partial failure.
    calls: optional list that receives every prompt the server sees,
    so a test can assert how much work a pass did.
    """
    choose = vector_for or default_vector

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            text = body.get("prompt", "")
            if calls is not None:
                calls.append(text)
            vector = choose(text)
            if vector is None:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps({"embedding": vector}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/embed" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def agent_on_path(testcase, directory, name="my-agent"):
    """Put an executable stub named `name` in <directory>/bin and that
    bin first on PATH for the test: a start's preflight resolves the
    agent command's executable, and fixtures name a stand-in agent
    that must resolve without being a real one."""
    bindir = pathlib.Path(directory) / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / name
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    patcher = mock.patch.dict(os.environ, {
        "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return stub


# The fake tmux executable: records its argv in $FAKE_TMUX_LOG and serves
# a scripted pane capture; a test writes it to a file and marks it executable.
_FAKE_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
n=$(wc -l < "$FAKE_TMUX_LOG")
if [ "$1" = load-buffer ]; then cat > "${FAKE_TMUX_STDIN:-/dev/null}"; fi
# FAKE_TMUX_FAIL_CALL: fail every call of this subcommand;
# FAKE_TMUX_FAIL_NTH: fail these 1-based call indexes (space separated)
if [ -n "${FAKE_TMUX_FAIL_CALL:-}" ] && [ "$1" = "$FAKE_TMUX_FAIL_CALL" ]; then exit 1; fi
case " ${FAKE_TMUX_FAIL_NTH:-} " in *" $n "*) exit 1;; esac
# FAKE_TMUX_HANG_NTH: these call indexes hang until the caller times out
case " ${FAKE_TMUX_HANG_NTH:-} " in *" $n "*) exec sleep 10;; esac
# FAKE_TMUX_PANE2 replaces the pane from call FAKE_TMUX_PANE_AFTER + 1 on
pane="$FAKE_TMUX_PANE"
if [ -n "${FAKE_TMUX_PANE_AFTER:-}" ] && [ "$n" -gt "$FAKE_TMUX_PANE_AFTER" ]; then pane="$FAKE_TMUX_PANE2"; fi
for a in "$@"; do
  if [ "$a" = capture-pane ]; then cat "$pane" 2>/dev/null; fi
  if [ "$a" = -l ]; then sleep "${FAKE_TMUX_PASTE_DELAY:-0}"; fi
done
exit "${FAKE_TMUX_RC:-0}"
"""


class FakeChatUpstream:
    """The chat API an upstream chat server answers (docs/reference/
    chat-api.md: a hive node's, or another install's), over one home's
    `data/chat.db` through server/chat_api: what the console's proxy
    forwards to. 2.0.0 runs no per-cousin chat server, so the tests that
    exercise the upstream path serve this double on a loopback port.
    `deliver` and `notify` are chat_api's seams; nothing is typed
    anywhere."""

    def __init__(self, config, *, deliver=None, notify=None, port=0):
        import urllib.parse
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from cousin_lib.server import chat_api
        self.config = config
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise chat_api.BadRequest("JSON body must be an object")
                return body

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}
                home = upstream.config.home
                try:
                    if parsed.path == "/health":
                        return self._json(200, {"status": "ok", "slug": upstream.config.slug,
                                                "port": upstream.port})
                    if parsed.path == "/api/history":
                        return self._json(200, chat_api.history(home, query))
                    if parsed.path == "/api/search":
                        return self._json(200, chat_api.search(home, query))
                    self._json(404, {"error": "not found"})
                except (chat_api.BadRequest, ValueError) as err:
                    self._json(400, {"error": str(err)})

            def do_POST(self):
                cfg = upstream.config
                try:
                    if self.path == "/api/send":
                        return self._json(200, chat_api.send(cfg, self._body(), deliver=deliver))
                    if self.path == "/api/%s_reply" % cfg.slug:
                        return self._json(200, chat_api.reply(cfg, self._body()))
                    if self.path == "/api/reactions":
                        return self._json(200, chat_api.react(cfg.home, self._body(),
                                                              notify=notify))
                    if self.path == "/api/archive":
                        return self._json(200, chat_api.archive(cfg.home, self._body()))
                    self._json(404, {"error": "not found"})
                except (chat_api.BadRequest, ValueError) as err:
                    self._json(400, {"error": str(err)})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = None

    @property
    def port(self):
        return self.httpd.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
