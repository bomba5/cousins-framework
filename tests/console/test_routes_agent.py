"""routes_agent: a cousin's [agent] settings per lane and its other
cousin.toml settings, each through its one path: [agent] through
spawn.persist_agent_values (agent_settings.validate, the sdk model's
validating turn as a long operation, agent_settings.apply), the rest
through toml_edit.write_keys with a validate hook. No live model call:
the validating turn is patched."""
import json
import threading
import time
import tomllib
from unittest import mock

from cousin_lib import supervisor
from tests.console._harness import ConsoleCase

ACCOUNTS = ('[accounts.fleet]\nkind = "claude-login"\n\n'
            '[accounts.keyed]\nkind = "anthropic-key"\n\n'
            '[accounts.oc]\nkind = "opencode"\nproviders = ["openai"]\n')


class AgentCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        (self.root / "config" / "accounts.toml").write_text(ACCOUNTS)

    def agent(self, home):
        return tomllib.loads((home / "cousin.toml").read_text()).get("agent") or {}

    def wait_op(self, slug, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            op = self.get("/api/cousins/%s/op" % slug)[1]["op"]
            if op and op["status"] != "running":
                return op
            time.sleep(0.02)
        self.fail("the op did not finish")


class GetAgent(AgentCase):
    def test_the_sdk_lane_and_the_hold(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\naccount = "fleet"\n'
                                         'effort = "high"\n')
        supervisor.hold(home, "console")
        self.serve()
        status, body = self.get("/api/cousins/wren/agent")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["lane"], "sdk")
        self.assertTrue(body["held"])
        self.assertIn("tmux", body["kinds"])
        s = body["settings"]
        self.assertEqual(s["effort"]["value"], "high")
        self.assertEqual(s["account"]["choices"], ["host", "fleet", "keyed"])
        self.assertTrue(s["runner"]["readonly"])
        self.assertIn("sessions", s)
        self.assertNotIn("env_allow", s)

    def test_the_tmux_kind_shows_the_base_allowlist_and_the_hard_deny(self):
        self.cousin("wren", extra='\n[agent]\nrunner = "tmux"\nenv_allow = ["EDITOR"]\n')
        self.serve()
        body = self.get("/api/cousins/wren/agent")[1]
        row = body["settings"]["env_allow"]
        self.assertEqual(row["value"], ["EDITOR"])
        self.assertIn("PATH", row["base"])
        self.assertIn("LC_*", row["base"])
        self.assertEqual(row["deny_prefixes"], ["CLAUDE", "ANTHROPIC"])
        # a key or token account never runs a pane
        self.assertNotIn("keyed", body["settings"]["account"]["choices"])

    def test_a_tmux_legacy_cousin_has_no_agent_settings(self):
        self.cousin("wren")
        self.serve()
        body = self.get("/api/cousins/wren/agent")[1]
        self.assertEqual(body["lane"], "tmux-legacy")
        self.assertEqual(body["settings"], {})

    def test_an_unknown_cousin_is_404(self):
        self.serve()
        self.assertEqual(self.get("/api/cousins/nobody/agent")[0], 404)


class PostAgent(AgentCase):
    def test_several_keys_one_write_and_restart_to_apply(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        server = self.serve()
        seen = []
        server.listeners.append(lambda k, d: seen.append(k))
        with mock.patch("cousin_lib.spawn.validate_turn_out_of_process") as child:
            status, body = self.post("/api/cousins/wren/agent", {"changes": {
                "effort": "low", "auto_start": False, "rollover_at_percent": 60,
                "account": "fleet", "sessions": {"meeting": "own"}}})
        self.assertEqual(status, 200, body)
        child.assert_not_called()
        self.assertTrue(body["restart_required"])
        self.assertEqual(sorted(body["changed"]), ["account", "auto_start", "effort",
                                                  "rollover_at_percent", "sessions"])
        agent = self.agent(home)
        self.assertEqual((agent["effort"], agent["auto_start"], agent["account"]),
                         ("low", False, "fleet"))
        self.assertEqual(agent["sessions"], {"meeting": "own"})
        self.assertEqual(body["agent"]["settings"]["effort"]["value"], "low")
        self.assertIn("cousins-refresh", seen)

    def test_refusals_name_each_key_and_write_nothing(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        before = (home / "cousin.toml").read_bytes()
        status, body = self.post("/api/cousins/wren/agent", {"changes": {
            "effort": "ultra", "sessions": {"operator": "own"}, "runner": "fake",
            "account": "oc", "shell_env": ["X"]}})
        self.assertEqual(status, 400, body)
        self.assertEqual(set(body["errors"]),
                         {"effort", "sessions", "runner", "shell_env"})
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"account": "oc"}})
        self.assertEqual(status, 400, body)
        self.assertIn("opencode", body["errors"]["account"])
        self.assertEqual((home / "cousin.toml").read_bytes(), before)

    def test_a_same_value_save_changes_nothing(self):
        self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\neffort = "low"\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"effort": "low"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["changed"], [])
        self.assertFalse(body["restart_required"])

    def test_bad_bodies(self):
        self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        for bad in ({}, {"changes": {}}, {"changes": []}, {"changes": "effort"}):
            self.assertEqual(self.post("/api/cousins/wren/agent", bad)[0], 400, bad)
        for bad in ({"sessions": "own"}, {"sessions": ["peer"]}, {"auto_start": "no"},
                    {"rollover_at_percent": "80"}, {"env_allow": "EDITOR"}):
            status, body = self.post("/api/cousins/wren/agent", {"changes": bad})
            self.assertEqual(status, 400, (bad, body))
            self.assertIn(list(bad)[0], body["errors"])

    def test_a_tmux_legacy_cousin_is_refused(self):
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"effort": "low"}})
        self.assertEqual(status, 400, body)
        self.assertIn("tmux-legacy", body["error"])

    def test_an_sdk_model_is_validated_by_one_turn_as_a_long_operation(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\neffort = "low"\n')
        self.serve()
        gate = threading.Event()
        seen = []

        def turn(home_, root, model, effort, **kw):
            gate.wait(5)
            seen.append((model, effort))
            return 0, "validate: ok"
        with mock.patch("cousin_lib.spawn.validate_turn_out_of_process", turn):
            status, body = self.post("/api/cousins/wren/agent", {"changes": {
                "model": "m-two", "effort": "high"}})
            self.assertEqual(status, 202, body)
            self.assertEqual(body["op"]["kind"], "agent-settings")
            # the op holds the cousin: a second write waits its turn
            status2, body2 = self.post("/api/cousins/wren/agent", {"changes": {"auto_start": False}})
            self.assertEqual(status2, 409, body2)
            gate.set()
            op = self.wait_op("wren")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(seen, [("m-two", "high")])
        self.assertEqual(sorted(op["result"]["changed"]), ["effort", "model"])
        self.assertTrue(op["result"]["restart_required"])
        self.assertEqual(self.agent(home)["model"], "m-two")

    def test_a_failed_turn_fails_the_op_and_writes_nothing(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        before = (home / "cousin.toml").read_bytes()
        with mock.patch("cousin_lib.spawn.validate_turn_out_of_process",
                        return_value=(4, "validate: not_found_error")):
            status, body = self.post("/api/cousins/wren/agent", {"changes": {"model": "m-bad"}})
            self.assertEqual(status, 202, body)
            op = self.wait_op("wren")
        self.assertEqual(op["status"], "failed")
        self.assertIn("not_found_error", op["error"])
        self.assertEqual((home / "cousin.toml").read_bytes(), before)

    def test_an_opencode_model_is_checked_on_the_spot_with_no_turn(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "opencode"\naccount = "oc"\n'
                                         'model = "openai/gpt-4o"\n')
        self.serve()
        with mock.patch("cousin_lib.spawn.validate_turn_out_of_process") as child:
            status, body = self.post("/api/cousins/wren/agent", {"changes": {
                "model": "openai/gpt-5", "shell_env": ["EDITOR"]}})
            self.assertEqual(status, 200, body)
            for bad in ("mistral/large", "gpt-5", "openai/claude-proxy"):
                status, body = self.post("/api/cousins/wren/agent",
                                         {"changes": {"model": bad}})
                self.assertEqual(status, 400, (bad, body))
                self.assertIn("model", body["errors"])
            status, body = self.post("/api/cousins/wren/agent",
                                     {"changes": {"shell_env": ["ANTHROPIC_API_KEY"]}})
            self.assertEqual(status, 400, body)
        child.assert_not_called()
        self.assertEqual(self.agent(home)["model"], "openai/gpt-5")
        self.assertEqual(self.agent(home)["shell_env"], ["EDITOR"])

    def test_the_tmux_kinds_env_allow_and_the_hard_deny(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "tmux"\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"env_allow": ["EDITOR"]}})
        self.assertEqual(status, 200, body)
        status, body = self.post("/api/cousins/wren/agent",
                                 {"changes": {"env_allow": ["CLAUDE_CONFIG_DIR"]}})
        self.assertEqual(status, 400, body)
        self.assertIn("hard deny", body["errors"]["env_allow"])
        self.assertEqual(self.agent(home)["env_allow"], ["EDITOR"])

    def test_a_running_long_operation_refuses_a_write(self):
        from cousin_lib.console import longop
        self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        server = self.serve()
        hold = longop.exclusive(server, "wren", "migrate")
        self.addCleanup(hold.release)
        status, body = self.post("/api/cousins/wren/agent", {"changes": {"auto_start": False}})
        self.assertEqual(status, 409, body)


class CousinSettings(AgentCase):
    def test_get_shows_values_defaults_and_the_read_only_rows(self):
        (self.root / "config" / "harness.toml").write_text(
            '[agent]\ncommit_attribution = false\n')
        self.cousin("wren", extra='\n[memory]\nreview_batch = 5\n'
                                  '\n[session]\nstart_hooks = ["echo hi"]\n')
        self.serve()
        status, body = self.get("/api/cousins/wren/settings")
        self.assertEqual(status, 200, body)
        f = body["fields"]
        self.assertEqual(f["cousin.name"]["value"], "Wren")
        self.assertEqual(f["cousin.peer_visible"]["value"], True)
        self.assertEqual(f["memory.review_batch"]["value"], 5)
        self.assertEqual(f["memory.proactive_recall"]["default"], True)
        self.assertIsNone(f["memory.review_model"]["value"])
        self.assertIsNone(f["lifecycle.flip_at"]["value"])
        self.assertEqual(f["lifecycle.flip_at"]["effective"], "04:00")
        ca = f["agent.commit_attribution"]
        self.assertIsNone(ca["value"])
        self.assertEqual(ca["install"], {"value": False, "source": "config/harness.toml [agent]"})
        self.assertFalse(ca["effective"])
        ro = body["readonly"]
        self.assertNotIn("chat.port", ro)       # no per-cousin chat server
        self.assertEqual(ro["session.start_hooks"], ["echo hi"])
        self.assertEqual(ro["session.end_hooks"], [])

    def test_the_install_default_with_no_harness_file_is_the_built_in_one(self):
        self.cousin("wren")
        self.serve()
        ca = self.get("/api/cousins/wren/settings")[1]["fields"]["agent.commit_attribution"]
        self.assertEqual(ca["install"]["value"], True)
        self.assertIn("built-in", ca["install"]["source"])

    def test_post_writes_each_key_and_says_what_needs_a_restart(self):
        home = self.cousin("wren", extra='\n[memory]\nscope = "shared"\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/settings", {"changes": {
            "cousin.peer_visible": False, "memory.review_batch": 0,
            "lifecycle.flip_at": "never", "memory.review_model": "m-two"}})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["restart_required"])
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["cousin"]["peer_visible"], False)
        self.assertEqual(data["memory"], {"scope": "shared", "review_batch": 0,
                                          "review_model": "m-two"})
        self.assertEqual(data["lifecycle"]["flip_at"], "never")
        status, body = self.post("/api/cousins/wren/settings", {"changes": {
            "cousin.name": "Wrenna", "memory.proactive_recall": False,
            "lifecycle.flip_at": None}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["restart_required"])
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["cousin"]["name"], "Wrenna")
        self.assertNotIn("flip_at", data.get("lifecycle", {}))

    def test_refusals(self):
        home = self.cousin("wren")
        self.serve()
        before = (home / "cousin.toml").read_bytes()
        status, body = self.post("/api/cousins/wren/settings", {"changes": {
            "cousin.name": " ", "cousin.peer_visible": "no", "memory.review_batch": -1,
            "lifecycle.flip_at": "25:00", "memory.review_model": "two words",
            "agent.commit_attribution": "false", "chat.port": 9000,
            "session.start_hooks": ["rm -rf /"], "cousin.slug": "x"}})
        self.assertEqual(status, 400, body)
        self.assertEqual(set(body["errors"]), {
            "cousin.name", "cousin.peer_visible", "memory.review_batch", "lifecycle.flip_at",
            "memory.review_model", "agent.commit_attribution", "chat.port",
            "session.start_hooks", "cousin.slug"})
        self.assertIn("not a setting", body["errors"]["chat.port"])
        self.assertEqual((home / "cousin.toml").read_bytes(), before)

    def test_commit_attribution_on_a_runner_cousin(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"agent.commit_attribution": False}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["restart_required"])
        self.assertEqual(self.agent(home), {"runner": "sdk", "commit_attribution": False})
        f = body["settings"]["fields"]["agent.commit_attribution"]
        self.assertEqual((f["value"], f["effective"]), (False, False))
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"agent.commit_attribution": None}})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.agent(home), {"runner": "sdk"})

    def test_commit_attribution_on_a_tmux_cousin_reaches_its_harness_settings(self):
        """The tmux lanes carry the value in <home>/.claude/settings.json
        (harness_settings.apply_project_settings): cousin.toml alone would
        wait for a `cousin-spawn --repair-settings`."""
        home = self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"agent.commit_attribution": False}})
        self.assertEqual(status, 200, body)
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertIs(settings["includeCoAuthoredBy"], False)
        self.assertIn("harness settings", body.get("note", ""))

    def test_an_unknown_cousin_is_404(self):
        self.serve()
        self.assertEqual(self.get("/api/cousins/nobody/settings")[0], 404)
        self.assertEqual(self.post("/api/cousins/nobody/settings",
                                   {"changes": {"cousin.peer_visible": True}})[0], 404)


class SpawnOptionsLanes(AgentCase):
    """The spawn dialog's account list per kind is the settings panel's own
    lane check (agent_settings.check_lane): the tmux kind refuses a key or
    token account, which accounts.check_lane alone lets through."""

    def test_a_key_account_is_not_offered_on_the_tmux_kind(self):
        self.serve()
        body = self.get("/api/spawn/options")[1]
        rows = {a["name"]: a for a in body["accounts"]}
        self.assertNotIn("tmux", rows["keyed"]["lanes"])
        self.assertIn("sdk", rows["keyed"]["lanes"])
        self.assertIn("tmux", rows["fleet"]["lanes"])

    def test_each_account_carries_the_model_suggestions_of_its_lanes(self):
        (self.root / "config" / "accounts.toml").write_text(
            ACCOUNTS + '\n[accounts.box]\nkind = "opencode"\n'
                       'endpoint = "http://127.0.0.1:11434/v1"\nendpoint_model = "qwen3"\n')
        self.serve()
        rows = {a["name"]: a for a in self.get("/api/spawn/options")[1]["accounts"]}
        self.assertEqual(rows["oc"]["models"], ["openai/"])
        self.assertEqual(len(rows["box"]["models"]), 1)
        self.assertTrue(rows["box"]["models"][0].endswith("/qwen3"))
        self.assertNotIn("models", rows["fleet"])


class AgentEdgeCases(AgentCase):
    """Edge cases of the [agent] and settings routes."""

    def test_model_and_account_together_run_the_turn_on_the_new_account(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        seen = []

        def turn(h, r, model, effort, **kw):
            seen.append((model, kw.get("account")))
            return 0, "ok"
        with mock.patch("cousin_lib.spawn.validate_turn_out_of_process", turn):
            status, body = self.post("/api/cousins/wren/agent", {"changes": {
                "model": "m-two", "account": "fleet"}})
            self.assertEqual(status, 202, body)
            op = self.wait_op("wren")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(seen, [("m-two", "fleet")])
        self.assertEqual(self.agent(home)["account"], "fleet")

    def test_a_failed_op_names_the_row_and_an_unexpected_error_is_worded(self):
        self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        with mock.patch("cousin_lib.spawn.validate_turn_out_of_process",
                        return_value=(4, "validate: nope")):
            self.post("/api/cousins/wren/agent", {"changes": {"model": "m-bad"}})
            op = self.wait_op("wren")
        self.assertEqual(list(op["result"]["errors"]), ["model"])
        from cousin_lib import spawn
        with mock.patch("cousin_lib.spawn.validate_turn_out_of_process",
                        return_value=(0, "ok")), \
                mock.patch("cousin_lib.agent_settings.apply",
                           side_effect=spawn.SpawnError("disk says no")):
            self.post("/api/cousins/wren/agent", {"changes": {"model": "m-three"}})
            op = self.wait_op("wren")
        self.assertEqual(op["status"], "failed")
        self.assertIn("disk says no", op["error"])
        self.assertIn("disk says no", op["result"]["errors"]["changes"])

    def test_describe_serves_the_model_rule_the_turn_flag_and_the_tmux_lane(self):
        self.cousin("wren", extra='\n[agent]\nrunner = "opencode"\naccount = "oc"\n'
                                  'model = "openai/gpt-5"\n')
        self.cousin("sam", extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        oc = self.get("/api/cousins/wren/agent")[1]
        sdk = self.get("/api/cousins/sam/agent")[1]
        self.assertEqual(oc["model_rule"]["placeholder"], "<provider>/<model>")
        self.assertTrue(oc["model_rule"]["provider_model"])
        self.assertFalse(oc["model_change_spends_turn"])
        self.assertTrue(sdk["model_change_spends_turn"])
        self.assertFalse(sdk["model_rule"]["provider_model"])
        self.assertEqual(sdk["tmux_lane"], "tmux-legacy")

    def test_commit_attribution_is_an_agent_key_every_lane_reads(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "tmux"\n')
        self.serve()
        self.assertIn("commit_attribution", self.get("/api/cousins/wren/agent")[1]["settings"])
        status, body = self.post("/api/cousins/wren/agent",
                                 {"changes": {"commit_attribution": "false"}})
        self.assertEqual(status, 400, body)
        status, body = self.post("/api/cousins/wren/agent",
                                 {"changes": {"commit_attribution": False}})
        self.assertEqual(status, 200, body)
        # the tmux kind reads it from its harness settings: written there too
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertIs(settings["includeCoAuthoredBy"], False)

    def test_the_operators_own_attribution_key_wins_and_the_note_says_so(self):
        home = self.cousin("wren")
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text('{"includeCoAuthoredBy": true}\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"agent.commit_attribution": False}})
        self.assertEqual(status, 200, body)
        self.assertIn("includeCoAuthoredBy", body["note"])
        self.assertIn("own", body["note"])

    def test_a_harness_settings_failure_reports_what_was_written(self):
        home = self.cousin("wren")
        self.serve()
        with mock.patch("cousin_lib.harness_settings.apply_project_settings",
                        side_effect=OSError("read-only file system")):
            status, body = self.post("/api/cousins/wren/settings",
                                     {"changes": {"agent.commit_attribution": False}})
        self.assertEqual(status, 500, body)
        self.assertIn("read-only file system", body["error"])
        self.assertEqual(body["written"], {"agent.commit_attribution": False})
        self.assertIs(tomllib.loads((home / "cousin.toml").read_text())["agent"]
                      ["commit_attribution"], False)

    def test_an_unreadable_install_value_makes_the_effective_one_unknown(self):
        (self.root / "config" / "harness.toml").write_text('[agent]\ncommit_attribution = "x"\n')
        self.cousin("wren")
        self.serve()
        f = self.get("/api/cousins/wren/settings")[1]["fields"]["agent.commit_attribution"]
        self.assertIsNone(f["install"]["value"])
        self.assertIsNone(f["effective"])

    def test_an_int_percentage_equal_to_the_files_float_is_no_change(self):
        self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\nrollover_at_percent = 80.0\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/agent",
                                 {"changes": {"rollover_at_percent": 80}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["changed"], [])


class BrokenTablesBlockOnlyTheirWrites(AgentCase):
    """The settings route checks the [agent] table only when
    it writes an [agent] key; a stale [agent] never blocks a rename."""

    def test_a_broken_agent_table_blocks_only_an_agent_write(self):
        home = self.cousin("wren", extra='\n[agent]\nrunner = "sdk"\naccount = "gone"\n')
        self.serve()
        status, body = self.post("/api/cousins/wren/settings", {"changes": {
            "cousin.name": "Wrenna", "memory.review_batch": 4}})
        self.assertEqual(status, 200, body)
        self.assertEqual(tomllib.loads((home / "cousin.toml").read_text())["cousin"]["name"],
                         "Wrenna")
        before = (home / "cousin.toml").read_bytes()
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"agent.commit_attribution": False}})
        self.assertEqual(status, 400, body)
        self.assertIn("agent.account", body["errors"])
        self.assertIn("gone", body["errors"]["agent.account"])
        self.assertEqual((home / "cousin.toml").read_bytes(), before)

    def test_a_broken_install_attribution_blocks_only_an_attribution_write(self):
        (self.root / "config" / "harness.toml").write_text('[agent]\ncommit_attribution = "x"\n')
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/settings",
                                 {"changes": {"cousin.peer_visible": False}})
        self.assertEqual(status, 200, body)
