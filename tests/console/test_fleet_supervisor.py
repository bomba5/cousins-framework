"""The console's start, stop and restart on the runner lane go through
cousin-supervisor, and the fleet row carries the supervisor's view of
the cousin's runner (phase 6 task 2, R7, R10). The supervisor is a stub
answering its socket; the tmux lane's routes are covered, unchanged, by
test_routes_fleet."""
import os
import subprocess
import sys
import threading
import time
import tomllib
from unittest import mock

from cousin_lib.console import longop
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
        status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "slug": "wren", "status": "started"})
        self.assertEqual(stub.ops(), [("start", "wren")])
        self.assertEqual(self.tmux_calls(), "")
        self.assertFalse((self.root / "cousins" / "wren" / "data" / "chat-server.pid").exists())
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

    def test_a_start_during_a_no_wait_stop_is_never_silently_already_running(self):
        # item 2: after a 202 stopping, the runner can keep its lock up
        # to ~35s (delivery.is_alive still True) while the supervisor
        # already holds it (supervisor.is_held, written at once by the
        # stop). A Start in that window must not trust the stale alive
        # read: it must ask the supervisor and report its true answer -
        # here "still stopping" - never "already running" and never a
        # silent no-op that leaves the cousin down and held.
        from cousin_lib import supervisor
        home = self.cousin("wren", extra=RUNNER)
        stub = self.stub(start={"ok": False, "name": "runner:wren",
                                "error": "runner:wren is still stopping;"
                                         " start it once it is down"})
        self.serve()
        supervisor.hold(home, "console")
        with mock.patch("cousin_lib.delivery.is_alive", lambda home, **kw: True):
            status, body = self.post("/api/cousins/wren/start")
        self.assertNotEqual(body.get("status"), "already running", body)
        self.assertEqual(stub.ops(), [("start", "wren")])
        self.assertIn("still stopping", body.get("error", ""), body)

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

    def test_a_refused_stop_is_502_with_the_error_never_stopped(self):
        # N7: only `stopping` and stopped/not running are outcomes; a
        # supervisor that refused the stop is a bad gateway, with its reason
        self.cousin("wren", extra=RUNNER)
        self.stub(stop={"ok": False, "error": "the supervisor is stopping"})
        self.serve()
        status, body = self.post("/api/cousins/wren/stop")
        self.assertEqual(status, 502, body)
        self.assertEqual(body, {"ok": False, "slug": "wren",
                                "error": "cousin-supervisor refused the stop:"
                                         " the supervisor is stopping",
                                "runner": "unknown", "supervisor": "running"})
        self.assertNotIn("status", body)

    def _events(self, server):
        seen = []
        real = server.emit

        def emit(kind, payload):
            seen.append((kind, payload))
            return real(kind, payload)
        server.emit = emit
        return seen

    def test_a_stop_of_a_hand_started_runner_with_no_supervisor_says_so(self):
        # #92: the stop found no supervisor and a runner holding the lock
        # (started by hand): 502 "cousin-supervisor refused the stop: no
        # reason given" named a refusal nobody made
        from cousin_lib import supervisor
        home = self.cousin("wren", extra=RUNNER)
        server = self.serve()
        seen = self._events(server)
        with mock.patch("cousin_lib.delivery.is_alive", lambda home, **kw: True):
            status, body = self.post("/api/cousins/wren/stop")
        self.assertEqual(status, 503, body)
        self.assertNotIn("refused", body["error"])
        self.assertNotIn("no reason given", body["error"])
        self.assertIn("no cousin-supervisor is running", body["error"])
        self.assertIn("started outside one", body["error"])
        self.assertIn("held", body["error"])
        self.assertEqual((body["runner"], body["supervisor"], body["held"]),
                         ("running", "not running", True))
        self.assertTrue(supervisor.is_held(home))
        self.assertIn(("cousin-status", {"slug": "wren", "status": "stop failed"}), seen)

    def test_a_still_stopping_start_is_409(self):
        # #92: transient, the caller's to retry; it was a 500
        from cousin_lib import supervisor
        home = self.cousin("wren", extra=RUNNER)
        self.stub(start={"ok": False, "name": "runner:wren",
                         "error": "runner:wren is still stopping; start it once it is down"})
        server = self.serve()
        seen = self._events(server)
        supervisor.hold(home, "console")
        status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 409, body)
        self.assertIn("still stopping", body["error"])
        statuses = [p for k, p in seen if k == "cousin-status"]
        self.assertEqual([p["status"] for p in statuses], ["starting", "start failed"])
        self.assertIn("still stopping", statuses[-1]["error"])

    def test_a_start_beside_a_hand_started_runner_is_409_and_starts_nothing(self):
        # #92: held, a runner started by hand alive, a supervisor with no
        # child of its own running: "started" was answered for a second
        # runner that sat in backoff behind the first one's lock
        from cousin_lib import supervisor
        home = self.cousin("wren", extra=RUNNER)
        stub = self.stub()
        stub.write_snapshot({})
        self.serve()
        supervisor.hold(home, "console")
        with mock.patch("cousin_lib.delivery.is_alive", lambda home, **kw: True):
            status, body = self.post("/api/cousins/wren/start")
        self.assertEqual(status, 409, body)
        self.assertIn("did not start", body["error"])
        self.assertNotIn(("start", "wren"), stub.ops())

    def test_any_refused_start_is_a_start_failed_event(self):
        # #92: `starting` went out and nothing after it on a refusal
        self.cousin("wren", extra=RUNNER)
        server = self.serve()
        seen = self._events(server)
        status, body = self.post("/api/cousins/wren/start")      # no supervisor: 503
        self.assertEqual(status, 503, body)
        statuses = [p for k, p in seen if k == "cousin-status"]
        self.assertEqual([p["status"] for p in statuses], ["starting", "start failed"])
        self.assertEqual(statuses[-1]["error"], body["error"])

    def test_a_refused_stop_fails_a_restart_before_its_start(self):
        self.cousin("wren", extra=RUNNER)
        stub = self.stub(stop={"ok": False, "error": "the supervisor is stopping"})
        server = self.serve()
        server.settle_seconds = 0
        status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 502, body)
        self.assertIn("the supervisor is stopping", body["error"])
        self.assertEqual(stub.ops(), [("stop", "wren")])

    def test_a_stop_with_no_supervisor_is_200_stopped_and_held(self):
        # O9 through the console: nothing ran, the hold is written
        from cousin_lib import supervisor
        home = self.cousin("wren", extra=RUNNER)
        self.serve()
        status, body = self.post("/api/cousins/wren/stop")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "slug": "wren", "status": "stopped",
                                "runner": "not running", "supervisor": "not running",
                                "held": True})
        self.assertTrue(supervisor.is_held(home))

    def test_a_restart_with_no_supervisor_leaves_no_hold(self):
        # the restart's stop half holds, its start half is refused (503):
        # a restart asked for a running cousin, so the hold is released
        from cousin_lib import supervisor
        home = self.cousin("wren", extra=RUNNER)
        server = self.serve()
        server.settle_seconds = 0
        status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 503, body)
        self.assertTrue(body["stop"]["held"])
        self.assertFalse(supervisor.is_held(home))

    def test_a_refused_restart_keeps_an_earlier_operator_hold(self):
        # the cousin was already held by an earlier stop (the operator's
        # decision): a restart that cannot start it (no supervisor) must not
        # cancel that hold, nor rewrite who made it and when
        from cousin_lib import supervisor
        home = self.cousin("wren", extra=RUNNER)
        supervisor.hold(home, "cousin-supervisor stop")
        before = supervisor.held_path(home).read_text()
        server = self.serve()
        server.settle_seconds = 0
        status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 503, body)
        self.assertTrue(supervisor.is_held(home))
        self.assertEqual(supervisor.held_path(home).read_text(), before)

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
        # #98 review: the hold a restart writes names the restart, so the
        # resumed session is told to continue, not that a stop cut it
        self.assertEqual(stub.requests[0]["by"], "console restart")
        self.assertEqual(self.tmux_calls(), "")

    def test_restart_holds_the_mark_until_start_when_down_finishes(self):
        # fix round 2: the route answers 202 and returns, but its own
        # background half (_start_when_down) is still waiting on the
        # supervisor - the exclusive mark must stay held for that whole
        # window, released only once that half is done, not when the
        # route itself returns.
        self.cousin("wren", extra=RUNNER)
        gate = threading.Event()

        def status(req):
            gate.wait(5)
            return {"ok": True, "children": {}}

        stub = self.stub(status=status)
        server = self.serve()
        server.settle_seconds = 0
        status_code, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status_code, 202, body)
        deadline = time.monotonic() + 5
        while longop.op_running(server, "wren") != "restart" \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(longop.op_running(server, "wren"), "restart")
        with self.assertRaises(longop.Busy):
            longop.start(server, "wren", "migrate", lambda op: {})
        gate.set()
        deadline = time.monotonic() + 5
        while longop.op_running(server, "wren") is not None \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(longop.op_running(server, "wren"))
        self.assertEqual(stub.ops(), [("stop", "wren"), ("status", None), ("start", "wren")])

    def test_a_failed_handoff_releases_the_hold(self):
        # Fix round 3, Important 1: handed_off must flip only once
        # Thread.start() has actually returned; if it raises instead (a
        # thread the OS refused, say), the mark must not leak forever.
        self.cousin("wren", extra=RUNNER)
        self.stub()                     # default: stop -> "stopping"
        server = self.serve()
        server.settle_seconds = 0
        real_thread = threading.Thread

        class Boom:
            def start(self):
                raise RuntimeError("no threads left")

        def fake_thread(*a, **kw):
            if str(kw.get("name", "")).startswith("console-restart-"):
                return Boom()
            return real_thread(*a, **kw)

        with mock.patch("threading.Thread", side_effect=fake_thread):
            status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 500, body)
        self.assertIsNone(longop.op_running(server, "wren"))
        # released, not stuck: a second restart goes through normally,
        # never 409 "a restart is running on wren"
        status, body = self.post("/api/cousins/wren/restart")
        self.assertEqual(status, 202, body)

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
        # round 4: the reason rides along, so the console can say why
        self.assertEqual(self.row("wren")["supervisor"],
                         {"state": "failing", "reason": "configuration (exit 2)"})
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


class TestCreateRunnerCousin(_Case):
    """POST /api/cousins takes `runner` and `account` (phase 6 task 2)."""

    def setUp(self):
        super().setUp()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            "# {{NAME}} ({{SLUG}})\n{{ROLE_ONE_LINE}}\n"
            "{{ROLE_PARAGRAPH}}\n## Voice\n{{VOICE_GUIDE}}\n")
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.metered]\nkind = "anthropic-key"\n')

    def test_post_with_runner_and_account_creates_a_runner_cousin(self):
        from cousin_lib import supervisor
        self.serve()
        status, body = self.post("/api/cousins", {
            "slug": "toki", "role": "r", "voice": "v",
            "runner": "fake", "account": "metered"})
        self.assertEqual(status, 201, body)
        data = tomllib.loads((self.root / "cousins" / "toki" / "cousin.toml").read_text())
        self.assertEqual(data["agent"], {"runner": "fake", "account": "metered"})
        self.assertEqual([c.slug for c in supervisor.runner_cousins(self.root)], ["toki"])

    def test_empty_runner_and_account_are_the_default(self):
        self.serve()
        status, body = self.post("/api/cousins", {
            "slug": "toki", "role": "r", "voice": "v",
            "runner": "", "account": None})
        self.assertEqual(status, 201, body)
        data = tomllib.loads((self.root / "cousins" / "toki" / "cousin.toml").read_text())
        self.assertEqual(data["agent"], {"runner": "sdk"})   # R4: the default kind

    def test_a_bad_runner_or_account_is_400_and_nothing_is_created(self):
        self.serve()
        for extra in ({"runner": "pane"}, {"runner": 5}, {"account": ["metered"]},
                      {"runner": "sdk", "account": "nobody"}):
            payload = dict({"slug": "toki", "role": "r", "voice": "v"}, **extra)
            status, body = self.post("/api/cousins", payload)
            self.assertEqual(status, 400, (extra, body))
            self.assertFalse((self.root / "cousins" / "toki").exists(), extra)
