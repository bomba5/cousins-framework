"""Decision register routes: the list (it only reads), the operator's
answer under the session user, the manual retry, a decisions-refresh
event per answer and retry, and the fleet rows' open count, read once
per listing."""
import threading
import unittest
from unittest import mock

from cousin_lib import decisions
from cousin_lib.runner.inbox import Inbox
from tests.console._harness import ConsoleCase

RUNNER = '\n[agent]\nrunner = "fake"\n'


class TestDecisions(ConsoleCase):
    def test_list_answer_and_events(self):
        home = self.cousin("wren", operator="Priya", extra=RUNNER)
        server = self.serve()
        seen = []
        server.listeners.append(lambda k, d: seen.append((k, d)))
        self.assertEqual(self.get("/api/decisions"), (200, {"decisions": [], "open": 0}))
        self.assertFalse(decisions.db_path(self.root).exists())
        decisions.ask("wren", "Which cache?", options=["a: keep", "b: drop"],
                      recommendation="a", root=self.root)
        decisions.ask("wren", "Which port?", root=self.root)
        status, body = self.get("/api/decisions")
        self.assertEqual((status, body["open"]), (200, 2))
        self.assertEqual([d["id"] for d in body["decisions"]], [1, 2])
        self.assertEqual(self.get("/api/decisions?status=odd")[0], 400)
        self.assertEqual(self.post("/api/decisions/1/answer", {})[0], 400)
        self.assertEqual(self.post("/api/decisions/1/answer", {"option": "z"})[0], 400)
        self.assertEqual(self.post("/api/decisions/1/answer", {"option": 3})[0], 400)
        self.assertEqual(self.post("/api/decisions/9/answer", {"option": "a"})[0], 404)
        self.assertEqual(self.post("/api/decisions/x/answer", {"option": "a"})[0], 404)
        status, body = self.post("/api/decisions/1/answer", {"option": "b", "answer": "now"})
        self.assertEqual(status, 200, body)
        row = body["decision"]
        self.assertEqual((row["status"], row["answered_by"], row["answered_via"],
                          row["delivery"]), ("answered", "operator", "the console", "delivered"))
        self.assertEqual(self.post("/api/decisions/1/answer", {"option": "a"})[0], 400)
        [turn] = Inbox(home).open_rows("decision")
        self.assertEqual(turn["thread_id"], "operator:Priya")
        _, body = self.get("/api/decisions?status=open")
        self.assertEqual(([d["id"] for d in body["decisions"]], body["open"]), ([2], 1))
        self.assertEqual([d for k, d in seen if k == "decisions-refresh"],
                         [{"id": 1, "op": "answer"}])

    def test_the_list_only_reads_and_the_retry_route_redelivers(self):
        home = self.cousin("wren", operator="Priya")          # no runner: refused
        self.serve()
        decisions.ask("wren", "Which?", root=self.root)
        _, body = self.post("/api/decisions/1/answer", {"answer": "this one"})
        self.assertEqual(body["decision"]["delivery"], "pending")
        with mock.patch.object(decisions, "redeliver", side_effect=AssertionError("wrote")):
            status, body = self.get("/api/decisions")
        self.assertEqual(status, 200)
        row = body["decisions"][0]
        self.assertEqual((row["remembered"], row["delivered"]), (True, False))
        toml = home / "cousin.toml"
        toml.write_text(toml.read_text() + RUNNER)
        status, body = self.post("/api/decisions/redeliver", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(decisions.show(1, root=self.root)["delivered"])
        self.assertEqual(len(Inbox(home).open_rows("decision")), 1)

    def test_withdraw_has_no_console_route(self):
        self.cousin("wren", extra=RUNNER)
        self.serve()
        decisions.ask("wren", "Which?", root=self.root)
        self.assertEqual(self.post("/api/decisions/1/withdraw", {})[0], 404)
        self.assertEqual(decisions.show(1, root=self.root)["status"], "open")

    def test_the_fleet_row_carries_the_open_count(self):
        self.cousin("wren", extra=RUNNER)
        self.cousin("sam", extra=RUNNER)
        self.serve()
        decisions.ask("wren", "One?", root=self.root)
        decisions.ask("wren", "Two?", root=self.root)
        _, body = self.get("/api/cousins")
        rows = {c["slug"]: c for c in body["cousins"]}
        self.assertEqual((rows["wren"]["decisions_open"], rows["sam"]["decisions_open"]), (2, 0))
        self.assertEqual(rows["wren"]["decisions_stops"], [])

    def test_the_register_is_read_once_per_listing(self):
        from cousin_lib.console import routes_fleet
        for slug in ("wren", "sam", "ines"):
            self.cousin(slug, extra=RUNNER)
        server = self.serve()
        decisions.ask("sam", "One?", root=self.root)
        real, here = decisions.open_summary, threading.get_ident()
        calls = []

        def counted(*a, **kw):
            if threading.get_ident() == here:          # not the event poller's
                calls.append(1)
            return real(*a, **kw)
        with mock.patch.object(decisions, "open_summary", side_effect=counted):
            rows = {r["slug"]: r for r in routes_fleet.fleet_rows(server)}
        self.assertEqual(len(calls), 1)
        self.assertEqual(rows["sam"]["decisions_open"], 1)


if __name__ == "__main__":
    unittest.main()
