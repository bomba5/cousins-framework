"""The lane on the fleet: the spawn options carry the runner kinds, the
accounts and the keys each lane reads; the spawn route puts a runner
cousin's model in [agent]; the fleet row says which lane, account, hold,
auto start and login wait a cousin has (audit defects 1 and 2, WP0)."""
import json
import tomllib
import unittest
from unittest import mock

from cousin_lib import delivery
from cousin_lib.runner import auth
from tests.console._harness import ConsoleCase

ACCOUNTS = ('[accounts.fleet]\nkind = "claude-login"\n\n'
            '[accounts.oc]\nkind = "opencode"\nproviders = ["openai"]\n')


class SpawnOptions(ConsoleCase):
    def test_the_runner_kinds_accounts_and_lane_keys(self):
        (self.root / "config" / "accounts.toml").write_text(ACCOUNTS)
        self.serve()
        status, body = self.get("/api/spawn/options")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["runners"], list(delivery.RUNNER_KINDS))
        claude_lanes = [k for k in delivery.RUNNER_KINDS if k != "opencode"]
        self.assertEqual(body["accounts"], [
            {"name": "host", "kind": "claude-login", "lanes": claude_lanes},
            {"name": "fleet", "kind": "claude-login", "lanes": claude_lanes},
            {"name": "oc", "kind": "opencode", "lanes": ["opencode"]}])
        self.assertIsNone(body["accounts_error"])
        self.assertIn("effort", body["lane_keys"]["sdk"])
        self.assertNotIn("effort", body["lane_keys"]["opencode"])
        self.assertNotIn("model", body["lane_keys"]["fake"])
        self.assertIsNone(body["default_runner"])

    def test_the_kinds_follow_delivery(self):
        self.serve()
        with mock.patch.object(delivery, "RUNNER_KINDS", delivery.RUNNER_KINDS + ("tmux",)):
            body = self.get("/api/spawn/options")[1]
        self.assertIn("tmux", body["runners"])
        self.assertIn("env_allow", body["lane_keys"]["tmux"])

    def test_a_broken_accounts_file_is_reported_not_a_500(self):
        (self.root / "config" / "accounts.toml").write_text("[accounts.x]\nkind = 3\n")
        self.serve()
        status, body = self.get("/api/spawn/options")
        self.assertEqual(status, 200)
        self.assertEqual([a["name"] for a in body["accounts"]], ["host"])
        self.assertIn("kind", body["accounts_error"])


class SpawnARunnerCousin(ConsoleCase):
    def setUp(self):
        super().setUp()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            "# {{NAME}}\n{{ROLE_ONE_LINE}}\n{{VOICE_GUIDE}}\n")
        (self.root / "config" / "accounts.toml").write_text(ACCOUNTS)

    def test_runner_account_and_model_land_in_agent(self):
        self.serve()
        status, body = self.post("/api/cousins", {
            "slug": "toki", "role": "tester", "voice": "plain", "port": 8123,
            "runner": "sdk", "account": "fleet", "model": "m-one", "effort": "low"})
        self.assertEqual(status, 201, body)
        data = tomllib.loads((self.root / "cousins" / "toki" / "cousin.toml").read_text())
        self.assertNotIn("runtime", data)
        self.assertEqual(data["agent"], {"runner": "sdk", "account": "fleet",
                                         "model": "m-one", "effort": "low"})

    def test_a_lane_refusal_is_a_400_that_writes_nothing(self):
        self.serve()
        status, body = self.post("/api/cousins", {
            "slug": "toki", "role": "tester", "voice": "plain", "port": 8123,
            "runner": "sdk", "account": "oc"})
        self.assertEqual(status, 400, body)
        self.assertIn("opencode", body["error"])
        self.assertFalse((self.root / "cousins" / "toki").exists())


class SpawnTheTmuxLaneExplicitly(ConsoleCase):
    """Fix round 1, Important 7: the dialog's tmux-legacy choice is sent as
    runner "tmux-legacy", so COUSIN_DEFAULT_RUNNER cannot override it."""

    def setUp(self):
        super().setUp()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            "# {{NAME}}\n{{ROLE_ONE_LINE}}\n{{VOICE_GUIDE}}\n")

    def test_the_sentinel_beats_the_env_default(self):
        self.serve()
        with mock.patch.dict("os.environ", {"COUSIN_DEFAULT_RUNNER": "sdk"}):
            status, body = self.post("/api/cousins", {
                "slug": "toki", "role": "tester", "voice": "plain", "port": 8123,
                "runner": "tmux-legacy", "model": "m-one"})
            self.assertEqual(status, 201, body)
            data = tomllib.loads((self.root / "cousins" / "toki" / "cousin.toml").read_text())
            self.assertNotIn("agent", data)
            self.assertEqual(data["runtime"]["model"], "m-one")
            status, body = self.post("/api/cousins", {
                "slug": "toko", "role": "tester", "voice": "plain", "port": 8124,
                "runner": "tmux-legacy", "account": "fleet"})
            self.assertEqual(status, 400, body)
            self.assertIn("needs a runner", body["error"])

    def test_options_say_how_each_lane_takes_a_model(self):
        self.serve()
        body = self.get("/api/spawn/options")[1]
        self.assertEqual(body["tmux_lane"], "tmux-legacy")
        self.assertTrue(body["lane_models"]["opencode"]["required"])
        self.assertFalse(body["lane_models"]["opencode"]["catalogue"])
        self.assertIn("<provider>/<model>", body["lane_models"]["opencode"]["hint"])
        self.assertTrue(body["lane_models"]["sdk"]["catalogue"])
        self.assertFalse(body["lane_models"]["sdk"]["required"])
        self.assertNotIn("fake", body["lane_models"])


class RowLaneFields(ConsoleCase):
    def row(self, slug):
        return next(c for c in self.get("/api/cousins")[1]["cousins"] if c["slug"] == slug)

    def test_a_tmux_cousin(self):
        self.cousin("tess")
        self.serve()
        row = self.row("tess")
        self.assertEqual((row["lane"], row["account"], row["held"], row["autoStart"],
                          row["loginRequired"]),
                         ("tmux-legacy", None, False, None, None))

    def test_a_runner_cousin_held_off_auto_start_and_waiting_on_a_login(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "fake"\naccount = "fleet"\n'
                                         'auto_start = false\n')
        (home / "run").mkdir()
        (home / "run" / "held").write_text("2026-09-24T10:00:00Z console\n")
        auth.write_login_required(home, host="build-host", account="fleet", kind="claude-login",
                                  reason=auth.LOGIN, detail="Invalid API key sk-ant-SECRET",
                                  action="cousin-account login fleet")
        self.serve()
        row = self.row("wren")
        self.assertEqual((row["lane"], row["account"], row["held"], row["autoStart"]),
                         ("fake", "fleet", True, False))
        self.assertEqual(row["loginRequired"]["action"], "cousin-account login fleet")
        self.assertEqual(row["loginRequired"]["reason"], "login_required")
        self.assertIn("since", row["loginRequired"])
        # the action line only: the detail (the API's words) never leaves
        self.assertNotIn("SECRET", json.dumps(row))
        self.assertEqual(set(row["loginRequired"]), {"reason", "action", "since"})

    def test_a_default_runner_cousin(self):
        self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        row = self.row("wren")
        self.assertEqual((row["lane"], row["account"], row["held"], row["autoStart"],
                          row["loginRequired"]),
                         ("sdk", "host", False, True, None))


if __name__ == "__main__":
    unittest.main()
