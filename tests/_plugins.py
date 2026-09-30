"""A fake plugin for the plugin tests: a tiny stdlib HTTP service and a
tiny stdio MCP server, written into a temp directory, and the install's
config/plugins.toml naming it. Invented names only ("clock", "dial")."""
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

SERVICE = r'''
import http.server, json, os, sys, time, urllib.parse

PORT = int(os.environ["PLUGIN_PORT"])
DIR = os.environ["PLUGIN_DIR"]
GATE = os.path.join(DIR, "release")


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json", extra=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/healthz":
            return self._send(200, {"ok": True})
        if u.path.startswith("/page/"):
            return self._send(200, b"<p>page for " + u.path[6:].encode() + b"</p>",
                              "text/html; charset=utf-8", [("X-Fake", "1")])
        if u.path.startswith("/status/"):
            return self._send(int(u.path[8:]), {"status": int(u.path[8:])})
        if u.path == "/echo":
            return self._send(200, {"method": "GET", "path": u.path, "query": q,
                                    "env": {k: os.environ.get(k) for k in
                                            ("PLUGIN_PORT", "FRAMEWORK_ROOT", "CLOCK_MODE")},
                                    "connection": self.headers.get("Connection"),
                                    "cookie": self.headers.get("Cookie")})
        if u.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            deadline = time.time() + 20
            while not os.path.exists(GATE) and time.time() < deadline:
                time.sleep(0.02)
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        return self._send(201, {"method": "POST", "path": self.path,
                                "body": body.decode("utf-8", "replace"),
                                "type": self.headers.get("Content-Type")})


print("clock service on", PORT, flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
'''

MCP = r'''
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        result = {"protocolVersion": msg["params"].get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "clock", "version": "0.1.0"}}
    elif msg.get("method") == "tools/list":
        result = {"tools": [{"name": "now", "description": "the time",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {"content": [{"type": "text", "text": os.environ.get("COUSIN_SLUG", "")}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def manifest(name="clock", *, port=None, mcp=True, service=True, console=True, extra=""):
    text = 'name = "%s"\ndescription = "a fake %s"\nversion = "0.1.0"\n' % (name, name)
    if mcp:
        text += ('\n[mcp]\ncommand = %s\nargs = ["{plugin_dir}/mcp_server.py"]\n'
                 % json.dumps(sys.executable))
        text += 'env = { CLOCK_URL = "{service_url}", CLOCK_FOR = "{slug}" }\n' if service \
            else 'env = { CLOCK_FOR = "{slug}" }\n'
    if service:
        text += ('\n[service]\ncommand = %s\nargs = ["{plugin_dir}/server.py"]\n'
                 'env = { CLOCK_MODE = "fake" }\nport = %d\nhealth = "/healthz"\n'
                 % (json.dumps(sys.executable), port or free_port()))
    if console and service:
        text += '\n[console]\ntitle = "Clock"\npage = "/page/{slug}"\n'
    return text + extra


def write_plugin(root, name="clock", *, text=None, **kw):
    """<root>/plugins-local/<name> with plugin.toml, server.py and
    mcp_server.py; returns the directory."""
    directory = pathlib.Path(root) / "plugins-local" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.toml").write_text(text if text is not None else manifest(name, **kw))
    (directory / "server.py").write_text(SERVICE)
    (directory / "mcp_server.py").write_text(MCP)
    return directory


def declare(root, *names, enabled=None, raw=None):
    """config/plugins.toml naming each plugin by its root-relative path."""
    config = pathlib.Path(root) / "config"
    config.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (config / "plugins.toml").write_text(raw)
        return
    text = ""
    for name in names:
        text += '[plugins.%s]\npath = "plugins-local/%s"\n' % (name, name)
        if enabled is not None and name in enabled:
            text += "enabled = %s\n" % ("true" if enabled[name] else "false")
        text += "\n"
    (config / "plugins.toml").write_text(text)


def enable(home, *names):
    """Append [plugins] enabled to a cousin.toml."""
    path = pathlib.Path(home) / "cousin.toml"
    path.write_text(path.read_text() + "\n[plugins]\nenabled = %s\n" % json.dumps(list(names)))


def port_of(directory):
    import tomllib
    return tomllib.loads((pathlib.Path(directory) / "plugin.toml").read_text())["service"]["port"]


def start_service(test, directory, root):
    """Run the fake service as a process, like the supervisor would; waits
    for its health path. Stopped at cleanup."""
    port = port_of(directory)
    env = dict(os.environ, PLUGIN_PORT=str(port), PLUGIN_DIR=str(directory),
               FRAMEWORK_ROOT=str(root), CLOCK_MODE="fake")
    proc = subprocess.Popen([sys.executable, str(pathlib.Path(directory) / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop():
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
    test.addCleanup(stop)
    wait_healthy(port)
    return proc


def wait_healthy(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError("the fake service on %d never answered" % port)
        time.sleep(0.05)
