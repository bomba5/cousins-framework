"""The console's start, stop and restart on the runner lane go through
cousin-supervisor, and the fleet row carries the supervisor's view of
the cousin's runner (phase 6 task 2, R7, R10). The supervisor is a stub
answering its socket; the tmux lane's routes are covered, unchanged, by
test_routes_fleet."""
import os
import subprocess
import sys
import time
from unittest import mock

from tests._stub_supervisor import StubSupervisor
from tests.console._harness import ConsoleCase

RUNNER = '\n[agent]\nrunner = "fake"\n'


class _Case(ConsoleCase):
    def stub(self, **answers):
        stub = StubSupervisor(self.root, answers).start()
        self.addCleanup(stub.close)
        return stub

    def tmux_calls(self):
        return self.tmux_log.read_text() if self.tmux_log.exists() else ""

    def row(self, slug):
        _, body = self.get("/api/cousins")
        return next(r for r in body["cousins"] if r["slug"] == slug)


class TestRunnerLaneStartStop(_Case):
    def test_console_start_on_the_runner_lane_asks_the_supervisor_and_never_tmux(self):
        self.cousin("wren", extra=RUNNER)
        stub = self.stub()
        self.serve()
        chat_servers = []
        with mock.patch("cousin_lib.spawn._default_chat_server", chat_servers.append):
            status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "slug": "wren", "status": "started"})
        self.assertEqual(stub.ops(), [("start", "wren")])
        self.assertEqual(self.tmux_calls(), "")
        self.assertEqual(chat_servers, [])
        self.assertFalse((self.root / "config" / "agent-cmd").exists())

    def test_a_live_runner_is_already_running(self):
        self.cousin("wren", extra=RUNNER)
        stub = self.stub()
        self.serve()
        with mock.patch("cousin_lib.delivery.is_alive", lambda home, **kw: True):
            status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "already running")
        self.assertEqual(stub.requests, [])

    def test_start_without_a_supervisor_is_503_and_says_how(self):
        self.cousin("wren", extra=RUNNER)
        self.serve()
        status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 503, body)
        self.assertIn("cousin-supervisor run", body["error"])
        self.assertEqual(self.tmux_calls(), "")

    def test_clean_stop_on_the_runner_lane_is_202_stopping(self):
        # R6': the supervisor is asked without waiting; the fleet row's
        # supervisor.state shows when the runner is down
        self.cousin("wren", extra=RUNNER)
        stub = self.stub()
        server = self.serve()
        closes = []
        server.close_fn = lambda slug, **kw: closes.append(slug)
        status, body = self.post("/api/cousins/wren/stop")
        self.assertEqual(status, 202, body)
        self.assertEqual(body, {"ok": True, "slug": "wren", "status": "stopping",
                                "runner": "stopping", "supervisor": "running"})
        self.assertEqual(stub.ops(), [("stop", "wren")])
        self.assertEqual((stub.requests[0]["wait"], stub.requests[0]["by"]), (False, "console"))
        self.assertEqual(closes, [])
        self.assertEqual(self.tmux_calls(), "")

    def test_the_stop_route_never_waits_for_a_busy_runner(self):
        # I6: a supervisor that would hold a waited stop for the runner's
        # turn (here 5 s) does not hold the HTTP request
        self.cousin("wren", extra=RUNNER)

        def slow(req):
            if req.get("wait", True):
                time.sleep(5)
                return {"ok": True, "name": "runner:wren", "state": "stopped"}
            return {"ok": True, "name": "runner:wren", "state": "stopping"}

        self.stub(stop=slow)
        self.serve()
        began = time.monotonic()
        status, body = self.post("/api/cousins/wren/stop", {"clean": False})
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual((status, body["status"]), (202, "stopping"))

    def test_a_runner_with_nothing_to_stop_is_200_stopped(self):
        self.cousin("wren", extra=RUNNER)
        self.stub(stop={"ok": True, "name": "runner:wren", "state": "stopped"})
        self.serve()
        status, body = self.post("/api/cousins/wren/stop")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["status"], body["runner"]), ("stopped", "stopped"))

    def test_restart_on_the_runner_lane_is_202_then_started_when_down(self):
        self.cousin("wren", extra=RUNNER)
        stub = self.stub()                     # status: no child left, so down at once
        server = self.serve()
        server.settle_seconds = 0
        status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 202, body)
        self.assertEqual(body, {"ok": True, "slug": "wren", "status": "stopping",
                                "runner": "stopping", "supervisor": "running",
                                "target": "cousin/wren"})
        deadline = time.monotonic() + 10
        while ("start", "wren") not in stub.ops() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(stub.ops(), [("stop", "wren"), ("status", None), ("start", "wren")])
        self.assertEqual(self.tmux_calls(), "")

    def test_restart_with_nothing_to_stop_starts_at_once(self):
        self.cousin("wren", extra=RUNNER)
        stub = self.stub(stop={"ok": True, "name": "runner:wren", "state": "stopped"})
        server = self.serve()
        server.settle_seconds = 0
        status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["stop"]["runner"], "stopped")
        self.assertEqual(body["start"]["status"], "started")
        self.assertEqual(stub.ops(), [("stop", "wren"), ("start", "wren")])


class TestFleetRowSupervisor(_Case):
    def test_fleet_row_carries_the_supervisor_state(self):
        self.cousin("wren", extra=RUNNER)
        self.cousin("sam")                      # a tmux cousin: no child of the supervisor
        stub = StubSupervisor(self.root)
        self.addCleanup(stub.close)
        stub.write_snapshot({"runner:wren": {"state": "failing", "pid": None, "restarts": 5,
                                             "since": "2026-01-01T00:00:00+00:00",
                                             "reason": "configuration (exit 2)",
                                             "last_exit": "exit 2"}})
        self.serve()
        self.assertEqual(self.row("wren")["supervisor"], {"state": "failing"})
        self.assertIn("supervisor", self.row("sam"))
        self.assertIsNone(self.row("sam")["supervisor"])

    def test_fleet_row_supervisor_is_null_without_a_snapshot(self):
        self.cousin("wren", extra=RUNNER)
        self.serve()
        row = self.row("wren")
        self.assertIn("supervisor", row)
        self.assertIsNone(row["supervisor"])
        # a stale snapshot (no supervisor holds the lock behind it) is
        # unknown, never "stopped"
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(10)
        stub = StubSupervisor(self.root)
        self.addCleanup(stub.close)
        stub.write_snapshot({"runner:wren": {"state": "running"}}, pid=dead.pid, live=False)
        row = self.row("wren")
        self.assertIn("supervisor", row)
        self.assertIsNone(row["supervisor"])


class TestRestartSupervised(_Case):
    def test_restart_reports_supervised_under_the_supervisor(self):
        server = self.serve()
        server.exit_fn = lambda: None
        with mock.patch.dict(os.environ, {"COUSIN_SUPERVISED": "1"}):
            os.environ.pop("INVOCATION_ID", None)
            status, body = self.post("/api/admin/restart/framework")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["supervised"])
        time.sleep(0.8)                          # let the restart timer fire into exit_fn
