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


class TestLogs(ConsoleCase):
    def test_tail_across_cousins_with_ages(self):
        a = self.cousin("wren")
        b = self.cousin("toki")
        (a / "data" / "chat-server.log").write_text(
            "2020-01-01T00:00:00 first\nplain line\n")
        (b / "data" / "chat-server.log").write_text("toki says\n")
        self.serve()
        _, body = self.get("/api/logs")
        lines = body["lines"]
        self.assertEqual([l["cousin"] for l in lines],
                         ["toki", "wren", "wren"])
        self.assertEqual(lines[1]["unit"], "chat-server")
        self.assertEqual(lines[1]["level"], "info")
        self.assertGreater(lines[1]["t"], 10 ** 8)
        self.assertEqual(lines[2]["t"], 0)
        self.assertEqual(lines[2]["msg"], "plain line")
        _, body = self.get("/api/logs?cousin=wren&n=1")
        self.assertEqual([l["msg"] for l in body["lines"]], ["plain line"])
        self.assertEqual(self.get("/api/logs?cousin=nobody")[0], 404)


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


if __name__ == "__main__":
    unittest.main()
