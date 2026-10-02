"""agent_settings: cousin.toml [agent] per runner lane. The lanes are
delivery.RUNNER_KINDS (never a list of its own); each key says which
lanes read it; the values are checked by the validators the runner
itself uses (effort_of, accounts.check_lane and refuse_claude_name, the
[agent.sessions] parser, opencode's shell_env and model checks)."""
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from cousin_lib import agent_settings, delivery
from cousin_lib.config import EFFORT_LEVELS

ACCOUNTS = ('[accounts.metered]\nkind = "anthropic-key"\n\n'
            '[accounts.fleet]\nkind = "claude-login"\n\n'
            '[accounts.oc]\nkind = "opencode"\nproviders = ["openai"]\n\n'
            '[accounts.box]\nkind = "opencode"\nendpoint = "http://127.0.0.1:11434/v1"\n'
            'endpoint_model = "qwen3"\n')


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "accounts.toml").write_text(ACCOUNTS)

    def cousin(self, agent, slug="wren", extra=""):
        home = self.root / "cousins" / slug
        home.mkdir(parents=True, exist_ok=True)
        text = '[cousin]\nslug = "%s"\nname = "Wren"\nrole = "r"\n\n[chat]\nport = 8100\n' % slug
        if agent is not None:
            text += "\n[agent]\n" + agent
        (home / "cousin.toml").write_text(text + extra)
        return home

    def agent(self, home):
        return tomllib.loads((home / "cousin.toml").read_text()).get("agent") or {}


class Describe(_Case):
    def test_the_sdk_lane(self):
        home = self.cousin('runner = "sdk"\naccount = "fleet"\neffort = "high"\n'
                           '\n[agent.sessions]\npeer = "own"\n')
        d = agent_settings.describe(home, self.root)
        self.assertEqual(d["lane"], "sdk")
        self.assertEqual(d["kinds"], list(delivery.RUNNER_KINDS))
        s = d["settings"]
        for key in ("runner", "account", "auto_start", "model", "effort",
                    "rollover_at_percent", "sessions"):
            self.assertIn(key, s, key)
        for key in ("shell_env", "small_model", "opencode_bin", "env_allow"):
            self.assertNotIn(key, s, key)
        self.assertEqual(s["effort"]["value"], "high")
        self.assertEqual(s["effort"]["choices"], list(EFFORT_LEVELS))
        self.assertEqual(s["account"]["value"], "fleet")
        self.assertEqual(s["account"]["choices"], ["host", "fleet", "metered"])
        self.assertTrue(s["runner"]["readonly"])
        self.assertEqual((s["auto_start"]["value"], s["auto_start"]["set"]), (True, False))
        self.assertEqual(s["rollover_at_percent"]["value"], 80.0)
        self.assertEqual(s["sessions"]["value"]["peer"], "own")
        self.assertEqual(s["sessions"]["value"]["operator"], "primary")
        self.assertIn("operator", s["sessions"]["always_primary"])
        self.assertEqual(d["errors"], {})

    def test_the_opencode_lane(self):
        home = self.cousin('runner = "opencode"\naccount = "oc"\nmodel = "openai/gpt-5"\n'
                           'shell_env = ["LANG"]\n')
        d = agent_settings.describe(home, self.root)
        s = d["settings"]
        self.assertEqual(d["lane"], "opencode")
        for key in ("model", "small_model", "shell_env", "opencode_bin",
                    "opencode_models_fetch", "rollover_at_percent"):
            self.assertIn(key, s, key)
        for key in ("effort", "sessions"):
            self.assertNotIn(key, s, key)
        self.assertTrue(s["opencode_bin"]["readonly"])
        self.assertEqual(s["account"]["choices"], ["box", "oc"])
        self.assertEqual(s["shell_env"]["value"], ["LANG"])
        self.assertEqual(s["small_model"]["value"], "openai/gpt-5")   # defaults to the model

    def test_the_fake_lane_has_no_model(self):
        d = agent_settings.describe(self.cousin('runner = "fake"\n'), self.root)
        self.assertEqual(set(d["settings"]), {"runner", "account", "auto_start",
                                              "commit_attribution", "dreaming", "dreaming_at"})

    def test_a_tmux_cousin_is_the_legacy_lane_with_no_agent_settings(self):
        d = agent_settings.describe(self.cousin(None), self.root)
        self.assertEqual(d["lane"], agent_settings.TMUX_LEGACY)
        self.assertEqual(d["settings"], {})

    def test_the_kinds_are_read_from_delivery(self):
        home = self.cousin('runner = "bogus"\n')
        self.assertEqual(agent_settings.describe(home, self.root)["lane"],
                         agent_settings.TMUX_LEGACY)
        with mock.patch.object(delivery, "RUNNER_KINDS", delivery.RUNNER_KINDS + ("bogus",)):
            d = agent_settings.describe(home, self.root)
            self.assertEqual(d["lane"], "bogus")
            self.assertIn("bogus", d["kinds"])

    def test_the_tmux_kind_reads_model_effort_and_env_allow(self):
        """`tmux` is a runner kind; its pane takes --model and
        --effort, and its environment allowlist is its own key."""
        home = self.cousin('runner = "tmux"\nenv_allow = ["LANG"]\neffort = "high"\n')
        d = agent_settings.describe(home, self.root)
        self.assertEqual(d["lane"], "tmux")
        self.assertIn("tmux", d["kinds"])
        self.assertEqual(set(d["settings"]),
                         {"runner", "account", "auto_start", "model", "effort", "env_allow",
                          "commit_attribution", "dreaming", "dreaming_at"})
        self.assertEqual(d["settings"]["env_allow"]["value"], ["LANG"])
        self.assertEqual(d["settings"]["effort"]["value"], "high")
        self.assertEqual(d["settings"]["account"]["choices"], ["host", "fleet"])   # no key account

    def test_a_broken_current_value_is_reported_not_raised(self):
        home = self.cousin('runner = "sdk"\naccount = "oc"\neffort = "ultra"\n')
        d = agent_settings.describe(home, self.root)
        self.assertIn("account", d["errors"])
        self.assertIn("effort", d["errors"])

    def test_summary_for_the_fleet_row(self):
        self.assertEqual(agent_settings.summary(self.cousin('runner = "sdk"\n')),
                         {"lane": "sdk", "account": "host", "autoStart": True})
        self.assertEqual(
            agent_settings.summary(self.cousin('runner = "fake"\naccount = "fleet"\n'
                                               'auto_start = false\n')),
            {"lane": "fake", "account": "fleet", "autoStart": False})
        self.assertEqual(agent_settings.summary(self.cousin(None)),
                         {"lane": "tmux-legacy", "account": None, "autoStart": None})


class CommitAttribution(_Case):
    """Every kind reads [agent] commit_attribution (runner/main
    commit_attribution_of), so every lane lists it, a bool or unset."""

    def test_every_lane_lists_it_and_refuses_a_non_bool(self):
        for kind in delivery.RUNNER_KINDS:
            self.assertIn("commit_attribution", agent_settings.lane_keys(kind), kind)
        home = self.cousin('runner = "sdk"\n')
        self.assertEqual(agent_settings.validate(home, self.root, {"commit_attribution": False}),
                         {"commit_attribution": False})
        with self.assertRaises(agent_settings.SettingsError) as ctx:
            agent_settings.validate(home, self.root, {"commit_attribution": "false"})
        self.assertIn("commit_attribution", ctx.exception.errors)

    def test_describe_serves_the_model_rule_and_the_turn_flag(self):
        d = agent_settings.describe(self.cousin('runner = "opencode"\naccount = "oc"\n'), self.root)
        self.assertTrue(d["model_rule"]["provider_model"])
        self.assertFalse(d["model_change_spends_turn"])
        self.assertEqual(d["tmux_lane"], agent_settings.TMUX_LEGACY)


class Validate(_Case):
    def refused(self, home, changes, key):
        with self.assertRaises(agent_settings.SettingsError) as cm:
            agent_settings.validate(home, self.root, changes)
        self.assertIn(key, cm.exception.errors, cm.exception.errors)
        return cm.exception.errors[key]

    def test_values_are_normalized_and_nothing_is_written(self):
        home = self.cousin('runner = "sdk"\n')
        before = (home / "cousin.toml").read_bytes()
        out = agent_settings.validate(home, self.root, {
            "effort": "max", "rollover_at_percent": 55, "auto_start": False,
            "account": "metered", "model": "claude-x", "sessions": {"peer": "own"}})
        self.assertEqual(out["rollover_at_percent"], 55.0)
        self.assertIsInstance(out["rollover_at_percent"], float)
        self.assertEqual(out["sessions"], {"peer": "own"})
        self.assertEqual((home / "cousin.toml").read_bytes(), before)

    def test_the_runners_own_checks_refuse(self):
        home = self.cousin('runner = "sdk"\n')
        self.assertIn("effort must be one of", self.refused(home, {"effort": "ultra"}, "effort"))
        self.assertIn("cannot be 'own'", self.refused(home, {"sessions": {"operator": "own"}},
                                                      "sessions"))
        self.assertIn("not a thread kind", self.refused(home, {"sessions": {"x": "own"}},
                                                        "sessions"))
        self.assertIn("kind opencode", self.refused(home, {"account": "oc"}, "account"))
        self.assertIn("not in config/accounts.toml",
                      self.refused(home, {"account": "nobody"}, "account"))
        self.assertIn("one word", self.refused(home, {"model": "two words"}, "model"))

    def test_types_and_bounds(self):
        home = self.cousin('runner = "sdk"\n')
        for bad in (0, 100.5, "80", True, [80]):
            self.refused(home, {"rollover_at_percent": bad}, "rollover_at_percent")
        self.refused(home, {"auto_start": "yes"}, "auto_start")

    def test_keys_off_the_lane_unknown_or_read_only_are_refused(self):
        home = self.cousin('runner = "sdk"\n')
        self.assertIn("opencode", self.refused(home, {"shell_env": ["LANG"]}, "shell_env"))
        self.assertIn("unknown", self.refused(home, {"nonsense": 1}, "nonsense"))
        self.assertIn("read-only", self.refused(home, {"runner": "fake"}, "runner"))
        oc = self.cousin('runner = "opencode"\naccount = "oc"\nmodel = "openai/gpt-5"\n',
                         slug="owl")
        self.assertIn("sdk", self.refused(oc, {"effort": "high"}, "effort"))
        self.assertIn("read-only", self.refused(oc, {"opencode_bin": "/bin/sh"}, "opencode_bin"))

    def test_the_opencode_lane_models_and_shell(self):
        home = self.cousin('runner = "opencode"\naccount = "oc"\nmodel = "openai/gpt-5"\n')
        agent_settings.validate(home, self.root, {"model": "openai/gpt-4o",
                                                   "shell_env": ["LANG", "TZ"]})
        self.assertIn("SDK and nowhere else", self.refused(home, {"model": "openai/claude-proxy"},
                                                  "model"))
        self.assertIn("keys for", self.refused(home, {"small_model": "mistral/large"},
                                               "small_model"))
        self.assertIn("<provider>/<model>", self.refused(home, {"model": "gpt-5"}, "model"))
        self.assertIn("required", self.refused(home, {"model": None}, "model"))
        self.assertIn("credentials", self.refused(home, {"shell_env": ["OPENAI_API_KEY"]},
                                                  "shell_env"))
        # the account and the model are checked together: box is a local endpoint
        self.assertIn("local", self.refused(home, {"account": "box"}, "model"))
        agent_settings.validate(home, self.root, {"account": "box", "model": "local/qwen3",
                                                   "small_model": None})
        self.assertIn("opencode", self.refused(home, {"account": "fleet"}, "account"))

    def test_the_tmux_kinds_env_allow_refuses_what_the_hard_deny_takes(self):
        """The console's check is the runner's (env_allow_of),
        so a name the pane would never get is refused where it is set."""
        home = self.cousin('runner = "tmux"\n')
        for name in ("GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_ENTRYPOINT"):
            self.assertIn("hard deny", self.refused(home, {"env_allow": [name]}, "env_allow"), name)
        agent_settings.validate(home, self.root, {"env_allow": ["PGHOST", "LANG"]})

    def test_a_tmux_kind_cousin_refuses_a_key_account(self):
        home = self.cousin('runner = "tmux"\n')
        self.assertIn("subscription login", self.refused(home, {"account": "metered"}, "account"))
        agent_settings.validate(home, self.root, {"account": "fleet", "effort": "low"})

    def test_a_tmux_cousin_has_nothing_to_validate_here(self):
        home = self.cousin(None)
        self.assertIn("tmux", self.refused(home, {"model": "m"}, "runner"))


class RunnerStartChecks(_Case):
    """What validate lets through, the runner's
    start must not refuse: the opencode bridge guard on the rendered config,
    and the account's preflight (a missing secret is a login to do, allowed)."""

    def refused(self, home, changes, key):
        with self.assertRaises(agent_settings.SettingsError) as cm:
            agent_settings.validate(home, self.root, changes)
        self.assertIn(key, cm.exception.errors, cm.exception.errors)
        return cm.exception.errors[key]

    def test_an_opencode_model_naming_the_bridge_is_refused(self):
        home = self.cousin('runner = "opencode"\naccount = "oc"\nmodel = "openai/gpt-5"\n')
        self.assertIn("bridge", self.refused(home, {"model": "openai/meridian-large"}, "model"))
        self.assertIn("bridge", self.refused(home, {"small_model": "openai/meridian-s"},
                                             "small_model"))

    def test_an_account_the_preflight_refuses_is_refused(self):
        home = self.cousin('runner = "sdk"\n')
        secret = self.root / ".secrets" / "accounts" / "metered"
        # missing: a login to do, allowed
        agent_settings.validate(home, self.root, {"account": "metered"})
        secret.parent.mkdir(parents=True)
        secret.parent.chmod(0o700)
        secret.write_text("bad key with spaces\n")
        secret.chmod(0o600)
        self.assertIn("malformed", self.refused(home, {"account": "metered"}, "account"))
        secret.write_text("sk-ant-api03-goodkeygoodkeygood\n")
        secret.parent.chmod(0o755)
        self.assertIn("open to group", self.refused(home, {"account": "metered"}, "account"))
        secret.parent.chmod(0o700)
        agent_settings.validate(home, self.root, {"account": "metered"})

    def test_the_model_error_names_the_agent_key(self):
        home = self.cousin('runner = "sdk"\n')
        text = self.refused(home, {"model": "two words"}, "model")
        self.assertIn("agent.model", text)
        self.assertNotIn("runtime.", text)


class Apply(_Case):
    def test_writes_every_key_in_one_write_and_keeps_the_rest(self):
        home = self.cousin('runner = "sdk"   # the lane\n', extra='\n[memory]\nscope = "shared"\n')
        d = agent_settings.apply(home, self.root, {
            "effort": "low", "rollover_at_percent": 70, "sessions": {"peer": "own",
                                                                      "meeting": "own"}})
        agent = self.agent(home)
        self.assertEqual(agent["effort"], "low")
        self.assertEqual(agent["rollover_at_percent"], 70.0)
        self.assertEqual(agent["sessions"], {"peer": "own", "meeting": "own"})
        text = (home / "cousin.toml").read_text()
        self.assertIn('runner = "sdk"   # the lane\n', text)
        self.assertIn('[memory]\nscope = "shared"\n', text)
        self.assertEqual(d["settings"]["effort"]["value"], "low")
        # primary is the default: a kind set back to it leaves the table
        agent_settings.apply(home, self.root, {"sessions": {"meeting": "primary"}})
        self.assertEqual(self.agent(home)["sessions"], {"peer": "own"})
        agent_settings.apply(home, self.root, {"effort": None})
        self.assertNotIn("effort", self.agent(home))

    def test_a_refusal_writes_nothing(self):
        home = self.cousin('runner = "sdk"\n')
        before = (home / "cousin.toml").read_bytes()
        with self.assertRaises(agent_settings.SettingsError):
            agent_settings.apply(home, self.root, {"effort": "low", "account": "oc"})
        self.assertEqual((home / "cousin.toml").read_bytes(), before)


class CheckNew(_Case):
    """The [agent] table a new cousin is spawned with."""

    def test_a_runner_cousins_model_and_effort(self):
        self.assertEqual(agent_settings.check_new(self.root, {"runner": "sdk", "model": "m",
                                                              "effort": "high"}),
                         {"runner": "sdk", "model": "m", "effort": "high"})

    def test_lane_rules_apply_at_spawn(self):
        with self.assertRaises(agent_settings.SettingsError) as cm:
            agent_settings.check_new(self.root, {"runner": "fake", "model": "m"})
        self.assertIn("model", cm.exception.errors)
        with self.assertRaises(agent_settings.SettingsError) as cm:
            agent_settings.check_new(self.root, {"runner": "sdk", "account": "oc"})
        self.assertIn("account", cm.exception.errors)
        with self.assertRaises(agent_settings.SettingsError) as cm:
            agent_settings.check_new(self.root, {"runner": "opencode", "account": "oc"})
        self.assertIn("model", cm.exception.errors)
        agent_settings.check_new(self.root, {"runner": "opencode", "account": "oc",
                                             "model": "openai/gpt-5"})

    def test_lane_keys_follow_the_kinds(self):
        self.assertIn("effort", agent_settings.lane_keys("sdk"))
        self.assertNotIn("effort", agent_settings.lane_keys("opencode"))
        self.assertEqual(agent_settings.lane_keys("tmux-legacy"), [])


if __name__ == "__main__":
    unittest.main()
