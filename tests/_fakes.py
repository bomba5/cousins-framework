"""Loopback fakes shared by the memory tests.

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
import stat
import threading
from unittest import mock


def default_vector(text):
    return [float(len(text) % 7), 1.0, 0.5]


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
