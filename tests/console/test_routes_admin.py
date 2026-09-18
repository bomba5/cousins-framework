import pathlib
"""Host stats, the chat-server log tail, and the console's own restart."""
import os
import socket
import time
import unittest
from unittest import mock

from tests.console._harness import ConsoleCase


class TestHost(ConsoleCase):
    def test_host_block_shape(self):
        self.serve()
        _, body = self.get("/api/host")
        self.assertEqual(body["host"], socket.gethostname())
        for key in ("kernel", "uptime", "cpu", "mem", "disk", "net",
                    "console_uptime"):
            self.assertIn(key, body)
        self.assertEqual(set(body["cpu"]), {"pct", "load1", "load5", "load15"})
        self.assertEqual(set(body["mem"]), {"total", "used", "cached"})
        self.assertEqual(set(body["net"]),
                         {"rx", "tx", "rx_total_gb", "tx_total_gb"})
        self.assertGreaterEqual(body["console_uptime"], 0)


class TestRestart(ConsoleCase):
    def test_answers_then_exits_through_the_seam(self):
        server = self.serve()
        exits = []
        server.exit_fn = lambda: exits.append(time.time())
        with mock.patch.dict(os.environ, {"INVOCATION_ID": "abc"}):
            before = time.time()
            status, body = self.post("/api/admin/restart/framework")
        self.assertEqual(body, {"ok": True, "target": "console",
                                "supervised": True, "eta_seconds": 4})
        self.assertEqual(exits, [])
        deadline = time.time() + 3
        while not exits and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(exits), 1)
        self.assertGreaterEqual(exits[0] - before, 0.5)
        with mock.patch.dict(os.environ, {}, clear=True):
            server.exit_fn = lambda: None
            _, body = self.post("/api/admin/restart/framework")
        self.assertFalse(body["supervised"])


class TestNoLogsRoute(ConsoleCase):
    """GET /api/logs served only the retired "tail logs" drawer and was
    removed with it; the canary fails if the route is registered
    again."""

    def test_the_route_is_gone(self):
        self.cousin("wren")
        self.serve()
        self.assertEqual(self.get("/api/logs")[0], 404)
        self.assertEqual(self.get("/api/logs?cousin=wren")[0], 404)


if __name__ == "__main__":
    unittest.main()


class TestRestartExitCode(unittest.TestCase):
    def test_the_restart_exit_is_a_failure_so_systemd_restarts_it(self):
        # Canary (2026-09-18): the button exited 0 and the unit only
        # restarts on failure, so pressing restart stopped the console.
        from cousin_lib.console import routes_admin
        self.assertNotEqual(routes_admin.RESTART_EXIT_CODE, 0)
        unit = (pathlib.Path(__file__).resolve().parents[2] / "systemd"
                / "cousin-console.service").read_text()
        self.assertIn("Restart=on-failure", unit)
