"""Telegram provisioning routes: the token is accepted and never
answered; a bridge is the supervisor's, and a cousin with no runner kind
has none (the config is written, `bridge` says why)."""
import json
import time
import unittest
from unittest import mock

from cousin_lib import telegram_admin
from tests._stub_supervisor import StubSupervisor
from tests.console._harness import ConsoleCase

TOKEN = "123456789:" + "B" * 35


class TestTelegramRoutes(ConsoleCase):
    def setUp(self):
        super().setUp()
        # Never the network: a fake bot and one pending Start press.
        for name, fake in (
                ("_get_me", lambda token: {"username": "WrenBot"}),
                ("_get_updates", lambda token: [{"update_id": 1,
                    "message": {"from": {"id": 42, "first_name": "Ana"}}}])):
            patch = mock.patch.object(telegram_admin, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def test_provision_flow_never_leaks_the_token(self):
        self.cousin("wren")
        self.serve()
        status, body = self.get("/api/cousins/wren/telegram")
        self.assertEqual((status, body["enabled"], body["token_set"],
                          body["ready"]), (200, False, False, "disabled"))
        status, body = self.post("/api/cousins/wren/telegram/token",
                                 {"token": TOKEN})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["token_set"])
        self.assertEqual(body["check"], {"ok": True, "bot": "WrenBot"})
        # first time: no operator yet, the Start press is offered anyway
        self.assertEqual([p["user_id"] for p in body["pending"]], [42])
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertEqual(self.post("/api/cousins/wren/telegram/token",
                                   {"token": "nope"})[0], 400)
        status, body = self.post("/api/cousins/wren/telegram/operators", {
            "operators": [{"user_id": 42, "name": "Ana"}]})
        self.assertEqual(body["operators"], [{"user_id": 42, "name": "Ana"}])
        status, body = self.post("/api/cousins/wren/telegram/enabled",
                                 {"enabled": True})
        from cousin_lib.delivery import lane_refusal
        self.assertEqual((body["enabled"], body["bridge"], body["running"]),
                         (True, lane_refusal(self.root / "cousins" / "wren"), False))
        self.assertIsNone(body["ready"])
        self.assertEqual(self.get("/api/cousins/nobody/telegram")[0], 404)



class TestRunnerLaneToggle(ConsoleCase):
    """A runner cousin's bridge is the supervisor's child: the
    console flips the config and asks the supervisor to rescan; it never
    starts a bridge of its own."""

    def setUp(self):
        super().setUp()
        for name, fake in (
                ("_get_me", lambda token: {"username": "WrenBot"}),
                ("_get_updates", lambda token: [])):
            patch = mock.patch.object(telegram_admin, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def runner_cousin(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "fake"\n')
        telegram_admin.set_token(home, self.root, "wren", TOKEN)
        telegram_admin.set_operators(home, [{"user_id": 42, "name": "Ana"}])
        return home

    def test_the_toggle_asks_the_supervisor_and_spawns_nothing(self):
        self.runner_cousin()
        self.tmux_running(True)          # whatever tmux says: not this cousin's lane
        stub = StubSupervisor(self.root, {"reload": {"ok": True, "added": ["telegram:wren"],
                                                     "removed": []}}).start()
        self.addCleanup(stub.close)
        self.serve()
        status, body = self.post("/api/cousins/wren/telegram/enabled", {"enabled": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["bridge"], "supervised")
        self.assertEqual(stub.ops(), [("reload", None)])
        status, body = self.post("/api/cousins/wren/telegram/operators", {
            "operators": [{"user_id": 42, "name": "Ana"}, {"user_id": 43, "name": "Bo"}]})
        self.assertEqual(status, 200, body)
        status, body = self.post("/api/cousins/wren/telegram/enabled", {"enabled": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(stub.ops(), [("reload", None)] * 3)

    def test_a_supervised_disable_leaves_the_stop_to_the_supervisor(self):
        self.runner_cousin()
        stub = StubSupervisor(self.root, {"reload": {"ok": True, "added": [],
                                                     "removed": ["telegram:wren"]}}).start()
        self.addCleanup(stub.close)
        self.serve()
        stops = []
        with mock.patch.object(telegram_admin, "stop_bridge",
                               lambda home, **kw: stops.append(home) or "stopped"):
            began = time.monotonic()
            status, body = self.post("/api/cousins/wren/telegram/enabled", {"enabled": False})
            took = time.monotonic() - began
        self.assertEqual(status, 200, body)
        self.assertEqual(body["bridge"], "supervised")
        self.assertEqual(stub.ops(), [("reload", None)])
        self.assertEqual(stops, [])
        self.assertLess(took, 2.0)

    def test_without_a_supervisor_a_disable_stops_the_bridge_itself(self):
        self.runner_cousin()
        self.serve()
        stops = []
        with mock.patch.object(telegram_admin, "stop_bridge",
                               lambda home, **kw: stops.append(home) or "stopped"):
            status, body = self.post("/api/cousins/wren/telegram/enabled", {"enabled": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(stops), 1)

    def test_a_slow_supervisor_is_not_a_missing_one(self):
        """Only no supervisor at all runs the console's own stop; a
        live one slower than the timeout does its rescan when it answers,
        and a second stop from the console would race it."""
        from cousin_lib import supervisor
        self.runner_cousin()
        self.serve()
        stops = []

        def slow(root, op, **kw):
            raise supervisor.SupervisorUnavailable(
                "the cousin-supervisor on %s did not answer within 10s" % root)
        with mock.patch.object(supervisor, "request", slow), \
                mock.patch.object(telegram_admin, "stop_bridge",
                                  lambda home, **kw: stops.append(home) or "stopped"):
            status, body = self.post("/api/cousins/wren/telegram/enabled", {"enabled": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(stops, [])
        self.assertIn("did not answer", body["bridge"])

    def test_token_and_operator_changes_report_the_bridge(self):
        self.runner_cousin()
        stub = StubSupervisor(self.root).start()
        stub.answers["reload"] = {"ok": True, "added": [], "removed": []}
        self.addCleanup(stub.close)
        self.serve()
        status, body = self.post("/api/cousins/wren/telegram/token", {"token": TOKEN})
        self.assertEqual((status, body["bridge"]), (200, "supervised"), body)
        status, body = self.post("/api/cousins/wren/telegram/operators", {
            "operators": [{"user_id": 42, "name": "Ana"}]})
        self.assertEqual((status, body["bridge"]), (200, "supervised"), body)

    def test_without_a_supervisor_the_toggle_still_starts_nothing(self):
        self.runner_cousin()
        self.tmux_running(True)
        self.serve()
        status, body = self.post("/api/cousins/wren/telegram/enabled", {"enabled": True})
        self.assertEqual(status, 200, body)
        self.assertIn("no cousin-supervisor", body["bridge"])


if __name__ == "__main__":
    unittest.main()
