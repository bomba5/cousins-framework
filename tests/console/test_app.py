"""The console server: guard first on every method, JSON conventions,
the route registry shared with later tasks, static serving with a
traversal check, and the entry point's root rule."""
import json
import os
import unittest
from unittest import mock

from cousin_lib.console import app, router
from tests.console._harness import ConsoleCase


class TestGuardAndConventions(ConsoleCase):
    def test_denied_address_is_403_on_every_method_including_delete(self):
        self.cousin("wren")
        self.serve(guard=lambda addr: False)
        for method, path in (("GET", "/api/cousins"), ("GET", "/"),
                             ("POST", "/api/cousins/wren/start"),
                             ("DELETE", "/api/cousins/wren")):
            status, body = self.request(method, path,
                                        {} if method != "GET" else None)
            self.assertEqual(status, 403, (method, path))

    def test_json_responses_carry_no_store(self):
        self.serve()
        status, headers, body = self.get("/api/cousins", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(headers.get("Content-Type", "").startswith(
            "application/json"))

    def test_malformed_json_is_400_never_an_empty_object(self):
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/role", "{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body, {"ok": False, "error": "malformed JSON"})

    def test_unknown_api_path_is_404_and_bad_slug_is_400(self):
        self.serve()
        self.assertEqual(self.get("/api/nothing")[0], 404)
        status, body = self.get("/api/cousins/Bad%20Slug/claude-md")
        self.assertEqual((status, body["error"]), (400, "bad slug"))

    def test_a_handler_exception_is_a_500_with_the_message(self):
        @router.route("GET", "/api/_boom")
        def boom(req):
            raise RuntimeError("kaboom")
        self.serve()
        status, body = self.get("/api/_boom")
        self.assertEqual(status, 500)
        self.assertIn("kaboom", body["error"])


class TestRegistryIsShared(ConsoleCase):
    def test_route_modules_are_a_list_later_tasks_extend(self):
        # Task 2 appends its own modules to this list; the server loads
        # every name at construction so a module registered after import
        # still serves.
        self.assertIsInstance(app.ROUTE_MODULES, list)
        for name in ("cousin_lib.console.routes_fleet",
                     "cousin_lib.console.routes_jobs",
                     "cousin_lib.console.routes_loops",
                     "cousin_lib.console.routes_memory",
                     "cousin_lib.console.routes_shared",
                     "cousin_lib.console.routes_admin",
                     "cousin_lib.console.routes_tracker",
                     "cousin_lib.console.auth"):
            self.assertIn(name, app.ROUTE_MODULES)

    def test_routes_survive_a_registry_clear_before_construction(self):
        router.clear()
        self.serve()
        self.assertEqual(self.get("/api/cousins")[0], 200)

    def test_a_route_reads_the_session_user_from_the_request(self):
        @router.route("GET", "/api/_whoami")
        def whoami(req):
            return 200, {"user": req.user, "root": str(req.server.root)}
        self.serve()
        status, body = self.get("/api/_whoami")
        self.assertEqual(body, {"user": None, "root": str(self.root)})

    def test_a_raw_response_lets_a_handler_serve_bytes(self):
        from cousin_lib.console.static import RawResponse

        @router.route("GET", "/api/_raw")
        def raw(req):
            return 200, RawResponse([("Content-Type", "text/plain"),
                                     ("Content-Length", "5")], b"hello")
        self.serve()
        status, headers, body = self.get("/api/_raw", raw=True)
        self.assertEqual((status, body), (200, b"hello"))
        self.assertEqual(headers["Content-Type"], "text/plain")

    def test_a_stream_is_written_chunk_by_chunk_and_closed(self):
        from cousin_lib.console import sse
        closed = []

        def chunks():
            try:
                yield b": one\n\n"
                yield b"data: {}\n\n"
            finally:
                closed.append(True)

        @router.route("GET", "/api/_stream")
        def stream(req):
            return 200, sse.Stream(chunks())
        self.serve()
        status, headers, body = self.get("/api/_stream", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(body, b": one\n\ndata: {}\n\n")
        self.assertEqual(closed, [True])

    def test_backend_b_modules_are_loaded_and_wired(self):
        for name in ("cousin_lib.console.proxy", "cousin_lib.console.pane",
                     "cousin_lib.console.sse"):
            self.assertIn(name, app.ROUTE_MODULES)
        from cousin_lib.console import sse
        self.cousin("wren")
        self.serve()
        # The duck-type the chat proxy reads: cousin resolved from
        # req.root, user required from req.query.
        status, body = self.get("/api/messages?cousin=wren")
        self.assertEqual(status, 400)
        self.assertIn("user", body["error"])
        self.assertTrue(sse._poller is not None and sse._poller.running)
        snap = sse.snapshot(root=self.root)
        self.assertIn("status", snap["cousins"][0])

    def test_events_hook_delivers_to_listeners(self):
        seen = []
        server = self.serve()
        server.listeners.append(lambda kind, data: seen.append((kind, data)))
        server.emit("tracker-change", {"id": 1, "op": "add"})
        self.assertEqual(seen, [("tracker-change", {"id": 1, "op": "add"})])


class TestStatic(ConsoleCase):
    def test_static_hook_is_replaceable_and_defaults_safely(self):
        self.serve()
        static = self.root / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><p>hi")
        (static / "secret.txt").write_text("no")
        (self.root / "outside.html").write_text("no")
        self.server.static_dir = static
        status, headers, body = self.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(self.get("/secret.txt", raw=True)[0], 404)
        self.assertNotEqual(self.get("/../outside.html", raw=True)[0], 200)
        self.assertEqual(self.get("/%2e%2e/outside.html", raw=True)[0],
                         403)

        def custom(handler, path):
            handler.send_json(200, {"custom": path})
        self.server.static_handler = custom
        self.assertEqual(self.get("/anything")[1], {"custom": "/anything"})

    def test_a_missing_static_dir_is_404_not_a_crash(self):
        self.serve()
        self.server.static_dir = self.root / "nope"
        self.assertEqual(self.get("/", raw=True)[0], 404)


class TestEntryPoint(ConsoleCase):
    def test_main_needs_a_root(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app.console_main(["--port", "0"]), 2)

    def test_cousin_ui_points_at_cousin_console(self):
        import contextlib
        import io
        from cousin_lib.ui import ui_main
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                contextlib.redirect_stderr(err):
            rc = ui_main(["--port", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("cousin-console", err.getvalue())

    def test_build_from_cli_flag_wins_over_env(self):
        self.cousin("wren")
        other = self.root / "decoy"
        (other / "cousins").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(other)}):
            server = app.build_console_from_cli(
                ["--root", str(self.root), "--port", "0",
                 "--tmux-bin", str(self.tmux)])
        self.addCleanup(server.stop)
        server.start()
        self.server = server
        _, body = self.get("/api/cousins")
        self.assertEqual([c["slug"] for c in body["cousins"]], ["wren"])
        self.assertEqual(os.environ.get("FRAMEWORK_ROOT"), str(self.root))


if __name__ == "__main__":
    unittest.main()


class TestCliServePath(ConsoleCase):
    """The CLI serves through ConsoleServer.serve_forever(), which wires
    the event sources first. Found live on the first real install: the
    CLI called the raw httpd loop, the snapshot carried bare registry
    rows with no status, and the sidebar drew every cousin as stopped
    while the Cousins view (the GET route) said running."""

    def test_cli_serve_path_wires_the_events_stream(self):
        import socket
        import subprocess
        import sys
        import time
        self.cousin("wren")
        self.tmux_running(True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "cousin_lib.console.app", "serve",
             "--root", str(self.root), "--port", str(port),
             "--tmux-bin", str(self.tmux)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ))
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        deadline = time.monotonic() + 15
        raw = b""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 1) as c:
                    c.sendall(b"GET /api/events HTTP/1.0\r\n"
                              b"Host: x\r\n\r\n")
                    c.settimeout(3)
                    while b"\n\n" not in raw.split(b"\r\n\r\n", 1)[-1]:
                        chunk = c.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                break
            except OSError:
                time.sleep(0.2)
        self.assertIn(b'"kind": "snapshot"', raw, raw[:400])
        body = raw.split(b"\r\n\r\n", 1)[1].decode()
        line = [l for l in body.splitlines() if l.startswith("data: ")][0]
        snap = json.loads(line[len("data: "):])["data"]
        self.assertEqual(snap["cousins"][0]["slug"], "wren")
        self.assertEqual(snap["cousins"][0]["status"], "running",
                         snap["cousins"][0])
