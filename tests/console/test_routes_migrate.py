"""routes_migrate (WP-B): cousin-migrate's plan, apply, check and rollback,
and the phase 11 kind switch (`--to sdk|tmux`), from the console. Every
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
    def test_a_plan_without_validate_answers_at_once_and_writes_nothing(self):
        before = (self.home / "cousin.toml").read_bytes()
        status, body = self.post("/api/cousins/wren/migrate/plan", {"account": "team"})
        self.assertEqual(status, 200, body)
        plan = body["plan"]
        self.assertEqual(plan["steps"], list(migrate.STEPS))
        checks = {c["check"]: c for c in plan["checks"]}
        self.assertFalse(checks["validate"]["ok"])      # a carried model wants one turn
        self.assertIn("--validate", checks["validate"]["detail"])
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertNotIn(("validate",), [c[:1] for c in self.live.calls])

    def test_validate_is_a_long_operation_that_spends_one_turn(self):
        status, body = self.post("/api/cousins/wren/migrate/plan",
                                 {"account": "team", "validate": True})
        self.assertEqual(status, 202, body)
        op = self.wait_done()
        self.assertEqual((op["kind"], op["status"]), ("migrate-plan", "done"), op)
        self.assertTrue(op["result"]["plan"]["ready"], op["result"]["plan"]["checks"])
        self.assertEqual(sum(1 for c in self.live.calls if c[0] == "validate"), 1)

    def test_the_kind_switch_plan_is_the_same_route_with_to(self):
        status, body = self.post("/api/cousins/wren/migrate/plan", {"to": "sdk"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["plan"]["to"], "sdk")
        kind = next(c for c in body["plan"]["checks"] if c["check"] == "kind")
        self.assertIn("legacy tmux lane", kind["detail"])

    def test_bad_bodies_are_400(self):
        for body in ({"to": "opencode"}, {"account": "Bad Name"}, {"validate": "yes"},
                     {"to": "sdk", "validate": True}, {"to": "sdk", "account": "team"}):
            status, _ = self.post("/api/cousins/wren/migrate/plan", body)
            self.assertEqual(status, 400, body)


class TestApply(MigrateCase):
    def test_apply_needs_a_confirmation(self):
        status, body = self.post("/api/cousins/wren/migrate/apply", {"account": "team"})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.live.calls, [])

    def test_apply_needs_the_supervisor(self):
        self.live.supervisor = False
        status, body = self.post("/api/cousins/wren/migrate/apply",
                                 {"account": "team", "validate": True, "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("supervisor", body["error"])

    def test_apply_runs_the_steps_as_stages(self):
        status, body = self.post("/api/cousins/wren/migrate/apply",
                                 {"account": "team", "validate": True, "confirm": True})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["params"]["steps"], ["plan"] + list(migrate.STEPS))
        op = self.wait_done()
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(self.stages(op), [("plan", "done")] + [(s, "done") for s in migrate.STEPS])
        self.assertEqual(self.agent()["runner"], "sdk")
        detail = dict((s["name"], s["detail"]) for s in op["stages"])
        self.assertIn("runner = 'sdk'", detail["toml"])
        # the stages came as they happened: close ran before import was reported
        names = [d["stage"]["name"] for k, d in self.events
                 if k == longop.EVENT and d["phase"] == "stage"]
        self.assertLess(names.index("close"), names.index("import"))

    def test_a_failed_step_fails_the_op_and_says_how_to_undo(self):
        self.live.start_error = "no child"
        self.post("/api/cousins/wren/migrate/apply",
                  {"account": "team", "validate": True, "confirm": True})
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertIn("start", op["error"])
        self.assertIn("roll back", op["error"])
        self.assertEqual(dict(self.stages(op))["start"], "failed")

    def test_a_plan_that_is_not_ready_changes_nothing(self):
        before = (self.home / "cousin.toml").read_bytes()
        self.post("/api/cousins/wren/migrate/apply", {"account": "team", "confirm": True})
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertIn("not ready", op["error"])
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertNotIn("close", [c[0] for c in self.live.calls])

    def test_one_migration_at_a_time_across_the_fleet(self):
        owl = self.cousin("owl")
        gate = threading.Event()
        self.addCleanup(gate.set)
        orig = self.live.close

        def slow_close(slug, root):
            gate.wait(5)
            return orig(slug, root)
        self.server.state["migrate.live"]["close"] = slow_close
        status, _ = self.post("/api/cousins/wren/migrate/apply",
                              {"account": "team", "validate": True, "confirm": True})
        self.assertEqual(status, 202)
        status, body = self.post("/api/cousins/owl/migrate/apply",
                                 {"to": "sdk", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("at a time", body["error"])
        self.assertTrue(body["busy"])
        self.assertTrue(owl.exists())
        gate.set()
        self.wait_done()

    def test_a_flip_running_on_the_cousin_refuses_the_apply(self):
        self.server.state.setdefault("flips", {})["wren"] = {"status": "running"}
        status, body = self.post("/api/cousins/wren/migrate/apply",
                                 {"account": "team", "validate": True, "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("flip", body["error"])


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
    def _migrate(self):
        self.post("/api/cousins/wren/migrate/apply",
                  {"account": "team", "validate": True, "confirm": True})
        self.assertEqual(self.wait_done()["status"], "done")

    def test_rollback_needs_a_confirmation_and_force_asks_twice(self):
        self._migrate()
        status, _ = self.post("/api/cousins/wren/migrate/rollback", {"which": "migration"})
        self.assertEqual(status, 400)
        status, body = self.post("/api/cousins/wren/migrate/rollback",
                                 {"which": "migration", "confirm": True, "force": True})
        self.assertEqual(status, 400, body)
        self.assertIn("second", body["error"])

    def test_rollback_restores_the_file(self):
        self._migrate()
        status, body = self.post("/api/cousins/wren/migrate/rollback",
                                 {"which": "migration", "confirm": True})
        self.assertEqual(status, 202, body)
        op = self.wait_done()
        self.assertEqual((op["kind"], op["status"]), ("migrate-rollback", "done"), op)
        self.assertEqual((self.home / "cousin.toml").read_bytes(), TOML.encode())
        self.assertIn("restore", dict(self.stages(op)))
        self.assertEqual(migrate.read_record(self.home)["state"], "rolled_back")

    def test_waiting_inbox_rows_refuse_without_force(self):
        self._migrate()
        from cousin_lib.delivery import Item
        from cousin_lib.runner.inbox import Inbox
        Inbox(self.home).put(Item(thread_id="person:ana", source="chat", sender="ana", body="hi"))
        self.post("/api/cousins/wren/migrate/rollback", {"which": "migration", "confirm": True})
        op = self.wait_done()
        self.assertEqual(op["status"], "failed")
        self.assertIn("--force", op["error"])
        self.post("/api/cousins/wren/migrate/rollback",
                  {"which": "migration", "confirm": True, "force": True, "force_confirm": True})
        op = self.wait_done()
        self.assertEqual(op["status"], "done", op)

    def test_rollback_needs_the_supervisor(self):
        self._migrate()
        self.live.supervisor = False
        status, body = self.post("/api/cousins/wren/migrate/rollback",
                                 {"which": "migration", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("supervisor", body["error"])

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
    def test_the_plan_says_the_one_time_trust_step(self):
        status, body = self.post("/api/cousins/wren/migrate/plan", {"to": "tmux"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["plan"]["ready"])
        trust = next(c for c in body["plan"]["checks"] if c["check"] == "trust")
        self.assertIn("accept the trust dialog", trust["detail"])

    def test_the_switch_reports_its_cursor_and_notice_stages(self):
        self.trust()
        status, body = self.post("/api/cousins/wren/migrate/apply", {"to": "tmux", "confirm": True})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "kind-switch")
        op = self.wait_done()
        self.assertEqual(op["status"], "done", op)
        self.assertEqual([n for n, _ in self.stages(op)],
                         ["trust", "close", "toml", "cursor", "start", "notice", "verify"])
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
        self.assertEqual([n for n, _ in self.stages(op)], ["close", "restore", "cursor", "start"])

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
