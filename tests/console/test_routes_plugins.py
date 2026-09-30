"""The console's plugin surface (docs/plugins.md): GET /api/plugins, the
fleet row's `plugins`, the per-cousin toggle, and the /plugins/<name>/
proxy (GET, POST, HEAD, status passthrough, 502 when down, an event
stream relayed as it comes, behind the login). The fake plugin's service
runs as a process, as the supervisor would run it. Invented cast only."""
import http.client
import socket
import time
from unittest import mock

from cousin_lib.console import auth
from tests import _plugins as fake
from tests.console._harness import ConsoleCase


class PluginConsoleCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.port = fake.free_port()
        self.dir = fake.write_plugin(self.root, "clock", port=self.port)
        fake.write_plugin(self.root, "dial", mcp=True, service=False, console=False)
        fake.declare(self.root, "clock", "dial")

    def runner_cousin(self, slug, *enabled, kind="fake"):
        home = self.cousin(slug, port=None, extra='\n[agent]\nrunner = "%s"\n' % kind)
        if enabled:
            fake.enable(home, *enabled)
        return home

    def row(self, slug):
        return next(c for c in self.get("/api/cousins")[1]["cousins"] if c["slug"] == slug)


class TestNoPlugins(ConsoleCase):
    def test_an_install_without_plugins_has_empty_answers(self):
        self.cousin("wren", port=None, extra='\n[agent]\nrunner = "fake"\n')
        self.serve()
        self.assertEqual(self.get("/api/plugins"),
                         (200, {"ok": True, "plugins": [], "problems": [],
                                "supervisor": False}))
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertEqual(row["plugins"], [])
        body = self.get("/api/cousins/wren/plugins")[1]
        self.assertEqual((body["available"], body["enabled"], body["plugins"]), ([], [], []))
        self.assertEqual(self.get("/plugins/clock/x")[0], 404)


class TestApi(PluginConsoleCase):
    def test_the_install_list(self):
        fake.write_plugin(self.root, "bell", text='name = "bell"\nshade = 1\n')
        fake.declare(self.root, "clock", "dial", "bell")
        self.runner_cousin("wren", "clock")
        self.serve()
        status, body = self.get("/api/plugins")
        self.assertEqual(status, 200)
        rows = {p["name"]: p for p in body["plugins"]}
        self.assertEqual(sorted(rows), ["clock", "dial"])
        clock = rows["clock"]
        self.assertEqual({k: clock[k] for k in ("mcp", "service", "console", "port", "title",
                                                "enabled_by", "supervisor", "healthy")},
                         {"mcp": True, "service": True, "console": True, "port": self.port,
                          "title": "Clock", "enabled_by": ["wren"], "supervisor": None,
                          "healthy": False})
        self.assertEqual((rows["dial"]["service"], rows["dial"]["healthy"]), (False, None))
        self.assertEqual([p["name"] for p in body["problems"]], ["bell"])
        fake.start_service(self, self.dir, self.root)
        clock = {p["name"]: p for p in self.get("/api/plugins")[1]["plugins"]}["clock"]
        self.assertIs(clock["healthy"], True)

    def test_the_row_carries_the_enabled_plugins_and_their_tabs(self):
        self.runner_cousin("wren", "clock", "dial", "ghost")
        self.runner_cousin("sam")
        self.serve()
        self.assertEqual(self.row("wren")["plugins"], [
            {"name": "clock", "title": "Clock", "description": "a fake clock",
             "tab": {"title": "Clock", "url": "/plugins/clock/page/wren", "placement": "pane"}},
            {"name": "dial", "title": None, "description": "a fake dial", "tab": None}])
        self.assertEqual(self.row("sam")["plugins"], [])

    def test_the_placement_is_carried_by_the_list_and_the_row(self):
        fake.write_plugin(self.root, "bell", placement="chat")
        fake.declare(self.root, "clock", "dial", "bell")
        self.runner_cousin("wren", "clock", "bell", "dial")
        self.serve()
        rows = {p["name"]: p for p in self.get("/api/plugins")[1]["plugins"]}
        self.assertEqual({n: r["placement"] for n, r in rows.items()},
                         {"bell": "chat", "clock": "pane", "dial": None})
        tabs = {p["name"]: p["tab"] for p in self.row("wren")["plugins"]}
        self.assertEqual(tabs["bell"], {"title": "Clock", "url": "/plugins/bell/page/wren",
                                        "placement": "chat"})
        self.assertEqual(tabs["clock"]["placement"], "pane")
        self.assertIsNone(tabs["dial"])
        body = self.get("/api/cousins/wren/plugins")[1]
        self.assertEqual({p["name"]: p["placement"] for p in body["available"]},
                         {"bell": "chat", "clock": "pane", "dial": None})

    def test_a_disabled_plugin_is_nowhere(self):
        fake.declare(self.root, "clock", "dial", enabled={"clock": False})
        self.runner_cousin("wren", "clock")
        self.serve()
        self.assertEqual([p["name"] for p in self.get("/api/plugins")[1]["plugins"]], ["dial"])
        self.assertEqual(self.row("wren")["plugins"], [])
        self.assertEqual(self.get("/plugins/clock/healthz")[0], 404)


class TestToggle(PluginConsoleCase):
    def test_enable_and_disable_write_cousin_toml(self):
        home = self.runner_cousin("wren")
        self.serve()
        body = self.get("/api/cousins/wren/plugins")[1]
        self.assertEqual([p["name"] for p in body["available"]], ["clock", "dial"])
        self.assertEqual(body["enabled"], [])
        status, body = self.post("/api/cousins/wren/plugins", {"enabled": ["dial", "clock"]})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["changed"], body["restart_required"]), (True, True))
        self.assertIsNone(body["reload"])                 # no supervisor runs here
        self.assertEqual(body["enabled"], ["clock", "dial"])
        self.assertIn('[plugins]\nenabled = ["clock", "dial"]',
                      (home / "cousin.toml").read_text())
        self.assertEqual([p["name"] for p in self.row("wren")["plugins"]], ["clock", "dial"])
        status, body = self.post("/api/cousins/wren/plugins", {"enabled": ["clock", "dial"]})
        self.assertEqual((body["changed"], body["restart_required"]), (False, False))
        status, body = self.post("/api/cousins/wren/plugins", {"enabled": []})
        self.assertEqual((status, body["enabled"], body["changed"]), (200, [], True))
        self.assertIn('[cousin]\nslug = "wren"', (home / "cousin.toml").read_text())

    def test_an_unknown_name_or_shape_is_refused(self):
        home = self.runner_cousin("wren")
        before = (home / "cousin.toml").read_text()
        self.serve()
        for payload in ({"enabled": ["ghost"]}, {"enabled": "clock"}, {}):
            self.assertEqual(self.post("/api/cousins/wren/plugins", payload)[0], 400, payload)
        self.assertEqual((home / "cousin.toml").read_text(), before)

    def test_a_tmux_cousin_is_told_the_gap(self):
        self.runner_cousin("wren", kind="tmux")
        self.serve()
        self.assertIn("tmux kind", self.get("/api/cousins/wren/plugins")[1]["note"])
        self.runner_cousin("sam")
        self.assertIsNone(self.get("/api/cousins/sam/plugins")[1]["note"])


class TestProxy(PluginConsoleCase):
    def setUp(self):
        super().setUp()
        self.runner_cousin("wren", "clock")
        self.serve()

    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        return conn.getresponse()

    def test_502_when_the_service_is_down(self):
        status, _, body = self.get("/plugins/clock/page/wren", raw=True)
        self.assertEqual(status, 502)
        self.assertIn(b"plugin clock: its service on 127.0.0.1:%d does not answer" % self.port,
                      body)

    def test_get_post_head_and_status_pass_through(self):
        fake.start_service(self, self.dir, self.root)
        status, headers, body = self.get("/plugins/clock/page/wren", raw=True)
        self.assertEqual((status, body), (200, b"<p>page for wren</p>"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["X-Fake"], "1")
        status, body = self.get("/plugins/clock/echo?a=1&b=two")
        self.assertEqual((status, body["path"], body["query"]), (200, "/echo",
                                                                 {"a": ["1"], "b": ["two"]}))
        self.assertIsNone(body["cookie"])                  # the console's session stays here
        status, body = self.request("POST", "/plugins/clock/submit?x=1", '{"hello": "clock"}')
        self.assertEqual((status, body["method"], body["path"], body["body"]),
                         (201, "POST", "/submit?x=1", '{"hello": "clock"}'))
        self.assertEqual(self.get("/plugins/clock/status/418")[0], 418)
        self.assertEqual(self.get("/plugins/clock/status/404")[0], 404)
        resp = self.raw("HEAD", "/plugins/clock/page/wren")
        self.assertEqual((resp.status, resp.read()), (200, b""))
        self.assertEqual(self.request("PUT", "/plugins/clock/page/wren")[0], 405)

    def test_an_unknown_plugin_is_404(self):
        self.assertEqual(self.get("/plugins/ghost/x")[0], 404)
        self.assertEqual(self.get("/plugins/dial/x")[0], 404)      # no [service]

    def test_the_body_is_bounded(self):
        # refused on the declared length, before a byte of the body is read
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /plugins/clock/submit HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Length: %d\r\n\r\n" % ((1 << 20) + 1))
        self.assertIn(b" 413 ", sock.recv(4096).split(b"\r\n")[0])

    def test_an_event_stream_is_relayed_as_it_comes(self):
        fake.start_service(self, self.dir, self.root)
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"GET /plugins/clock/events HTTP/1.1\r\nHost: x\r\n\r\n")
        got = b""
        deadline = time.monotonic() + 10
        while b"data: first\n\n" not in got and time.monotonic() < deadline:
            got += sock.recv(4096)
        head, _, rest = got.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n")[0])
        self.assertIn(b"Content-Type: text/event-stream", head)
        # the first event arrived while the service still holds the stream open
        self.assertEqual(rest, b"data: first\n\n")
        self.assertFalse((self.dir / "release").exists())
        (self.dir / "release").write_text("go")
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            rest += chunk
        self.assertEqual(rest, b"data: first\n\ndata: second\n\n")


    def test_a_stream_quiet_longer_than_the_connect_timeout_stays_open(self):
        """An event stream may be quiet for as long as it likes: the proxy
        asks the service for `Connection: close`, so http.client lets go of
        the socket at the headers and the connect timeout used to end the
        relay after that many quiet seconds."""
        from cousin_lib.console import routes_plugins
        patch = mock.patch.object(routes_plugins, "CONNECT_TIMEOUT_S", 0.3)
        patch.start(); self.addCleanup(patch.stop)
        fake.start_service(self, self.dir, self.root)
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b"GET /plugins/clock/events HTTP/1.1\r\nHost: x\r\n\r\n")
        got = b""
        deadline = time.monotonic() + 10
        while b"data: first\n\n" not in got and time.monotonic() < deadline:
            got += sock.recv(4096)
        time.sleep(1.0)                      # quiet for more than three connect timeouts
        (self.dir / "release").write_text("go")
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            got += chunk
        self.assertTrue(got.endswith(b"data: first\n\ndata: second\n\n"), got[-80:])

class TestProxyLogin(PluginConsoleCase):
    def test_a_session_is_required_like_every_page(self):
        auth.Users(self.root / "config" / "console-users.json").set_password(
            "ana", "correct horse")
        self.runner_cousin("wren", "clock")
        self.serve()
        fake.start_service(self, self.dir, self.root)
        self.assertEqual(self.get("/plugins/clock/page/wren")[0], 401)
        self.assertEqual(self.get("/api/plugins")[0], 401)
        self.post("/api/auth/login", {"user": "ana", "password": "correct horse"})
        status, _, body = self.get("/plugins/clock/page/wren", raw=True)
        self.assertEqual((status, body), (200, b"<p>page for wren</p>"))
        self.assertIsNone(self.get("/plugins/clock/echo")[1]["cookie"])

    def test_the_guard_applies(self):
        self.runner_cousin("wren", "clock")
        self.serve(guard=lambda addr: False)
        self.assertEqual(self.get("/plugins/clock/page/wren")[0], 403)
