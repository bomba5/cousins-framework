"""GET /api/health (docs/reference/console-api.md, "Health"): the loops
daemon's record and the supervisor's children not running, read-only,
behind the login like every other route."""
import unittest

from cousin_lib import health
from cousin_lib.console import auth
from tests._stub_supervisor import StubSupervisor
from tests.console._harness import ConsoleCase


class TestHealthRoute(ConsoleCase):
    def test_the_shape_with_a_failing_component_and_no_supervisor(self):
        health.record(self.root, [("dreaming:wren", False, "ImportError: x"),
                                  ("tick", True, None)], now=1_790_000_000.0)
        self.serve()
        status, body = self.get("/api/health")
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"components", "failing", "ok", "supervisor",
                                     "failing_count"})
        self.assertEqual(body["components"]["dreaming:wren"]["fails"], 1)
        row = body["failing"][0]
        self.assertEqual((row["key"], row["fails"], row["since"], row["error"], row["quiet"]),
                         ("dreaming:wren", 1, 1_790_000_000.0, "ImportError: x", True))
        self.assertEqual(body["ok"], ["tick"])
        self.assertEqual(body["supervisor"]["reachable"], False)
        self.assertEqual(body["failing_count"], 1)

    def test_a_child_not_running_counts(self):
        stub = StubSupervisor(self.root, {"status": {"ok": True, "children": {
            "runner:kestrel": {"state": "failing", "since": "2026-10-03T06:12:09+00:00",
                               "reason": "gave up"}}}}).start()
        self.addCleanup(stub.close)
        self.serve()
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["supervisor"]["failing"][0]["name"], "runner:kestrel")
        self.assertEqual((body["failing"], body["failing_count"]), ([], 1))

    def test_a_held_stop_is_left_out_of_the_top_bar_count(self):
        stub = StubSupervisor(self.root, {"status": {"ok": True, "children": {
            "runner:kestrel": {"state": "stopped", "since": "2026-10-06T10:56:32+00:00",
                               "reason": "stopped by request", "held": True},
            "runner:wren": {"state": "stopped", "since": "2026-10-06T11:00:00+00:00",
                            "reason": "login required (exit 4)", "held": False}}}}).start()
        self.addCleanup(stub.close)
        self.serve()
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual([c["name"] for c in body["supervisor"]["failing"]], ["runner:wren"])
        self.assertEqual(body["failing_count"], 1)

    def test_read_only_and_behind_the_login(self):
        self.serve()
        self.assertEqual(self.post("/api/health", {})[0], 405)
        auth.Users(self.root / "config" / "console-users.json").set_password(
            "ana", "correct horse")
        self.assertEqual(self.get("/api/health")[0], 401)


if __name__ == "__main__":
    unittest.main()
