"""routes_migrate: cousin-migrate's plan, apply, check and rollback, and
the kind switch (`--to sdk|tmux`), from the console. Every
live action (the clean stop, the supervisor, tmux, the account check, a
model turn) is injected through the server's test seams: this touches no
live home, no tmux, no supervisor and no model."""
import json
import os
import threading
import time
import tomllib

from cousin_lib import migrate
from cousin_lib.console import longop
from tests.console._harness import ConsoleCase
from tests.test_migrate import TOML, Live


class MigrateCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.home = self.cousin("wren")
        (self.home / "cousin.toml").write_bytes(TOML.encode())
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.team]\nkind = "claude-login"\nconfig_dir = "accounts/team"\n')
        self.live = Live()
        self.serve()
        self.server.state["migrate.live"] = self.live.kw()
        self.server.state["migrate.poll"] = 0.01
        self.events = []
        self.server.listeners.append(lambda kind, data: self.events.append((kind, data)))

    def wait_done(self, slug="wren", timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = longop.status(self.server, slug)
            if op and op["status"] != "running":
                return op
            time.sleep(0.02)
        self.fail("the op did not finish")

    def stages(self, op):
        return [(s["name"], s["status"]) for s in op["stages"]]

    def agent(self):
        return tomllib.loads((self.home / "cousin.toml").read_text()).get("agent", {})


class TestState(MigrateCase):
    def test_the_state_names_the_lane_the_kinds_and_what_is_not_built(self):
        status, body = self.get("/api/cousins/wren/migrate")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["lane"], "tmux-legacy")
        self.assertEqual(body["kinds"], list(migrate.SWITCH_KINDS))
        self.assertIsNone(body["migration"])
        self.assertIsNone(body["switch"])
        self.assertTrue(body["supervisor"])
        self.assertEqual({d["id"] for d in body["deferred"]}, {"adopt", "all"})

    def test_a_cousin_with_no_runner_carries_the_refusal_line(self):
        from cousin_lib.delivery import lane_refusal
        _, body = self.get("/api/cousins/wren/migrate")
        self.assertEqual(body["refusal"], lane_refusal(self.home))
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n\n[agent]\nrunner = "sdk"\n')
        _, body = self.get("/api/cousins/wren/migrate")
        self.assertIsNone(body["refusal"])

    def test_a_record_is_served_without_the_saved_file_bytes(self):
        (self.home / migrate.RECORD).write_text(json.dumps({
            "state": "migrated", "prior_toml_b64": "c2VjcmV0", "prior_mode": 416,
            "steps": [{"step": "close", "ok": True, "detail": "closed"}]}))
        status, body = self.get("/api/cousins/wren/migrate")
        self.assertEqual(body["migration"]["state"], "migrated")
        self.assertEqual(body["migration"]["steps"][0]["step"], "close")
        self.assertNotIn("prior_toml_b64", json.dumps(body))
        self.assertNotIn("prior_mode", json.dumps(body))

    def test_the_login_screen_is_the_screen_only(self):
        (self.home / "data" / "login-required.json").write_text(json.dumps(
            {"kind": "tmux", "screen": "trust", "ts": 1.0, "detail": "the API's words"}))
        _, body = self.get("/api/cousins/wren/migrate")
        self.assertEqual(body["loginScreen"]["screen"], "trust")
        self.assertNotIn("the API's words", json.dumps(body))

    def test_unknown_cousin_is_404(self):
        self.assertEqual(self.get("/api/cousins/nope/migrate")[0], 404)

    def test_the_screens_the_kinds_with_a_pane_and_the_op_kinds_are_served(self):
        _, body = self.get("/api/cousins/wren/migrate")
        self.assertEqual(body["pane_kinds"], ["tmux"])
        self.assertIn("trust", body["person_screens"])
        self.assertIn("login", body["person_screens"])
        self.assertEqual(body["pane_answers"], ["trust", "bypass", "mcp_approval"])
        self.assertIn("kind-switch", body["op_kinds"])
        self.assertIn("migrate-plan", body["op_kinds"])


class TestPlan(MigrateCase):
    def test_a_plan_without_to_is_refused_with_the_line(self):
        """2.0.0 keeps no conversion from the legacy lane: a
        plan without `to` on a cousin with no runner is a 409 carrying
        delivery.lane_refusal; nothing runs."""
        from cousin_lib.delivery import lane_refusal
        before = (self.home / "cousin.toml").read_bytes()
        for body in ({}, {"account": "team"}, {"account": "team", "validate": True}):
            status, answer = self.post("/api/cousins/wren/migrate/plan", body)
            self.assertEqual(status, 409, (body, answer))
            self.assertEqual(answer["error"], lane_refusal(self.home))
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertEqual(self.live.calls, [])
        self.assertIsNone(longop.status(self.server, "wren"))

    def test_the_kind_switch_plan_is_the_same_route_with_to(self):
        status, body = self.post("/api/cousins/wren/migrate/plan", {"to": "sdk"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["plan"]["to"], "sdk")
        kind = next(c for c in body["plan"]["checks"] if c["check"] == "kind")
        from cousin_lib.delivery import lane_refusal
        self.assertEqual(kind["detail"], lane_refusal(self.home))

    def test_bad_bodies_are_400(self):
        for body in ({"to": "opencode"}, {"account": "Bad Name"}, {"validate": "yes"},
                     {"to": "sdk", "validate": True}, {"to": "sdk", "account": "team"}):
            status, _ = self.post("/api/cousins/wren/migrate/plan", body)
            self.assertEqual(status, 400, body)


class TestApply(MigrateCase):
    def test_an_apply_without_to_is_refused_with_the_line(self):
        from cousin_lib.delivery import lane_refusal
        before = (self.home / "cousin.toml").read_bytes()
        for body in ({"account": "team"}, {"account": "team", "validate": True, "confirm": True}):
            status, answer = self.post("/api/cousins/wren/migrate/apply", body)
            self.assertEqual(status, 409, (body, answer))
            self.assertEqual(answer["error"], lane_refusal(self.home))
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertEqual(self.live.calls, [])
        self.assertIsNone(longop.status(self.server, "wren"))


class TestCheck(MigrateCase):
    def test_check_is_a_report(self):
        self.server.state["migrate.live"]["health"] = lambda home: (True, "answers")
        status, body = self.post("/api/cousins/wren/migrate/check", {})
        self.assertEqual(status, 200, body)
        self.assertIn("inbox", body["check"])
        self.assertEqual(body["check"]["chat"], "answers")

    def test_a_bad_since_is_400(self):
        status, _ = self.post("/api/cousins/wren/migrate/check", {"since": "yesterday"})
        self.assertEqual(status, 400)

    def test_check_with_validate_is_a_long_operation(self):
        self.server.state["migrate.live"]["health"] = lambda home: (True, "answers")
        status, body = self.post("/api/cousins/wren/migrate/check", {"validate": True})
        self.assertEqual(status, 202, body)
        op = self.wait_done()
        self.assertEqual(op["kind"], "migrate-check")
        self.assertIn("validate", op["result"]["check"])


class TestRollback(MigrateCase):
    def test_a_migration_rollback_is_refused_with_the_line(self):
        from cousin_lib.delivery import lane_refusal
        for body in ({"which": "migration"},
                     {"which": "migration", "confirm": True, "force": True,
                      "force_confirm": True}):
            status, answer = self.post("/api/cousins/wren/migrate/rollback", body)
            self.assertEqual(status, 409, (body, answer))
            self.assertEqual(answer["error"], lane_refusal(self.home))
        self.assertEqual(self.live.calls, [])
        self.assertIsNone(longop.status(self.server, "wren"))

    def test_bad_which_is_400(self):
        status, _ = self.post("/api/cousins/wren/migrate/rollback",
                              {"which": "everything", "confirm": True})
        self.assertEqual(status, 400)


class SwitchCase(MigrateCase):
    def setUp(self):
        super().setUp()
        self.config_dir = self.root / "accounts" / "team"
        self.config_dir.mkdir(parents=True)
        self.kind("sdk")
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "login"}))
        self.calls = []
        self.verified = (True, "a turn start under s-live")
        self.server.state["migrate.switch_live"] = self.switch_live()

    def kind(self, kind):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "%s"\n'
            'account = "team"\n' % kind)

    def trust(self):
        (self.config_dir / ".claude.json").write_text(json.dumps(
            {"projects": {str(self.home): {"hasTrustDialogAccepted": True}}}))

    def switch_live(self):
        def close(home, root):
            self.calls.append("close")

        def start(home, root):
            self.calls.append("start")

        def verify(home, root, session_id, to, since, **kw):
            self.calls.append(("verify", session_id, to))
            hook = getattr(self, "during_verify", None)
            if hook:
                hook()
            return self.verified

        def cursor_end(home, root, session_id, to):
            return 4096
        return dict(close=close, start=start, verify=verify, cursor_end=cursor_end,
                    supervisor_up=lambda root: True)


class TestSwitch(SwitchCase):
    def test_a_runner_cousin_without_to_is_told_to_name_a_kind(self):
        before = (self.home / "cousin.toml").read_bytes()
        for route, body in (("plan", {}), ("apply", {"confirm": True}),
                            ("rollback", {"which": "migration", "confirm": True})):
            status, answer = self.post("/api/cousins/wren/migrate/%s" % route, body)
            self.assertEqual(status, 400, (route, answer))
            self.assertIn("name a kind with --to (sdk, tmux)", answer["error"])
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_one_switch_at_a_time_across_the_fleet(self):
        self.trust()
        owl = self.cousin("owl")
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.during_verify = lambda: gate.wait(5)
        status, _ = self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        self.assertEqual(status, 202)
        status, body = self.post("/api/cousins/owl/migrate/apply",
                                 {"to": "sdk", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("at a time", body["error"])
        self.assertTrue(body["busy"])
        self.assertTrue(owl.exists())
        gate.set()
        self.wait_done()

    def test_a_flip_running_on_the_cousin_refuses_the_switch(self):
        self.server.state.setdefault("flips", {})["wren"] = {"status": "running"}
        status, body = self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("flip", body["error"])

    def test_the_plan_says_the_one_time_trust_step(self):
        status, body = self.post("/api/cousins/wren/migrate/plan", {"to": "tmux"})
        self.assertEqual(status, 200, body)
        # never a gate: the pane asks and verify waits for the operator
        self.assertTrue(body["plan"]["ready"], body)
        trust = next(c for c in body["plan"]["checks"] if c["check"] == "trust")
        self.assertTrue(trust["ok"])
        self.assertIn("the pane asks once", trust["detail"])

    def test_the_switch_reports_its_cursor_and_notice_stages(self):
        self.trust()
        status, body = self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "kind-switch")
        op = self.wait_done()
        self.assertEqual(op["status"], "done", op)
        self.assertEqual([n for n, _ in self.stages(op)],
                         ["trust", "close", "toml", "cursor", "notice", "start", "verify"])
        self.assertTrue(all(st == "done" for _, st in self.stages(op)))
        self.assertEqual(self.agent()["runner"], "tmux")

    def test_a_trust_dialog_during_verify_is_rendered_as_waiting(self):
        self.trust()
        seen = []

        def show_trust():
            (self.home / "data" / "login-required.json").write_text(
                json.dumps({"kind": "tmux", "screen": "trust", "ts": 1.0}))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                op = longop.status(self.server, "wren")
                verify = [s for s in op["stages"] if s["name"] == "verify"]
                if verify and verify[0]["detail"] and "trust" in verify[0]["detail"]:
                    seen.append(verify[0]["detail"])
                    return
                time.sleep(0.01)
        self.during_verify = show_trust
        self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        op = self.wait_done()
        self.assertEqual(op["status"], "done", op)
        self.assertTrue(seen, "the verify stage never said it waits on the trust dialog")
        self.assertIn("accept the trust dialog in the pane", seen[0])
        self.assertIn("screen: trust", seen[0])

    def test_a_failed_verify_fails_the_op_with_the_record_detail(self):
        self.trust()
        self.verified = (False, "no turn start under s-live within 90s")
        self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertEqual(dict(self.stages(op))["verify"], "failed")
        self.assertIn("no turn start", op["error"])

    def test_the_switch_rolls_back_to_the_kind_it_came_from(self):
        self.trust()
        before = (self.home / "cousin.toml").read_bytes()
        self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        self.wait_done()
        status, body = self.post("/api/cousins/wren/migrate/rollback",
                                 {"which": "switch", "to": "tmux", "confirm": True})
        self.assertEqual(status, 202, body)
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertIn("came from sdk", op["error"])
        self.post("/api/cousins/wren/migrate/rollback",
                  {"which": "switch", "to": "sdk", "confirm": True})
        op = self.wait_done()
        self.assertEqual((op["kind"], op["status"]), ("kind-switch-rollback", "done"), op)
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertEqual([n for n, _ in self.stages(op)], ["close", "notice", "restore", "cursor", "start"])

    def test_an_unexpected_error_marks_the_record_failed(self):
        self.trust()

        def broken_start(home, root):
            raise KeyError("children")
        self.server.state["migrate.switch_live"]["start"] = broken_start
        self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertIn("start", op["error"])
        self.assertIn("KeyError", op["error"])
        rec = json.loads((self.home / migrate.SWITCH_RECORD).read_text())
        self.assertEqual((rec["state"], rec["failed"]), ("failed", "start"))
        self.assertIn("KeyError", rec["error"])
        self.assertEqual(dict(self.stages(op))["start"], "failed")

    def test_the_verify_watcher_says_nothing_once_verify_returned(self):
        self.trust()
        self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        op = self.wait_done()
        (self.home / "data" / "login-required.json").write_text(
            json.dumps({"kind": "tmux", "screen": "trust", "ts": 1.0}))
        time.sleep(0.1)
        after = longop.status(self.server, "wren")
        self.assertEqual(after["stages"], op["stages"])

    def test_a_switch_rollback_takes_no_force(self):
        status, _ = self.post("/api/cousins/wren/migrate/rollback",
                              {"which": "switch", "to": "sdk", "confirm": True,
                               "force": True, "force_confirm": True})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    import unittest
    unittest.main()
