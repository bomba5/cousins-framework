"""Config seams: config/harness.toml.

The harness (the agent runtime that hosts a cousin) keeps this install's
session transcripts and its own auto-memory directory somewhere the
framework cannot know; that location is configuration. Absent means the
capability is off. Present but unparsable means a promise was made and
not kept, so it fails loud.
"""
import tempfile
import unittest
from pathlib import Path

from cousin_lib import config


class HarnessSeam(unittest.TestCase):
    def test_absent_file_means_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(config.harness_config(Path(root)))

    def test_reads_both_keys_and_expands_placeholders(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "harness.toml").write_text(
                'transcripts_dir = "/tmp/h/projects/{home_encoded}"\n'
                'auto_memory_dir = "/tmp/h/projects/{home_encoded}/memory"\n')
            cfg = config.harness_config(Path(root))
            home = Path("/srv/fw/cousins/testa/files")
            self.assertEqual(
                config.expand_harness_path(cfg["transcripts_dir"], home),
                Path("/tmp/h/projects/-srv-fw-cousins-testa-files"))
            self.assertEqual(
                config.expand_harness_path(cfg["auto_memory_dir"], home),
                Path("/tmp/h/projects/-srv-fw-cousins-testa-files/memory"))

    def test_missing_keys_read_as_none_not_a_default_path(self):
        # A partial file is half a promise: the named key works, the
        # other is None, never a guessed location.
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "harness.toml").write_text(
                'transcripts_dir = "/tmp/h/projects/{home_encoded}"\n')
            cfg = config.harness_config(Path(root))
            self.assertEqual(set(cfg), {"transcripts_dir", "auto_memory_dir",
                                        "settings_file", "host_label"})
            self.assertIsNone(cfg["host_label"])
            self.assertIsNone(cfg["auto_memory_dir"])
            self.assertIsNone(cfg["settings_file"])

    def test_home_encoded_is_the_harness_encoding_every_non_alphanumeric_is_a_dash(self):
        """Claude Code names a project dir by its path with every
        character that is not a letter or a digit turned into '-'
        (/tmp/tmpxpnoz7t_/cousins/wren is -tmp-tmpxpnoz7t--cousins-wren);
        mapping only '/' would give a home with '.', '_' or another
        character a transcripts dir that does not exist."""
        for home, encoded in (("/srv/fw/cousins/testa/files", "-srv-fw-cousins-testa-files"),
                              ("/tmp/tmpxpnoz7t_/cousins/wren", "-tmp-tmpxpnoz7t--cousins-wren"),
                              ("/srv/ana/.cousins/wren.v2", "-srv-ana--cousins-wren-v2"),
                              ("/srv/my fw/cousins/a+b", "-srv-my-fw-cousins-a-b")):
            with self.subTest(home=home):
                self.assertEqual(config.expand_harness_path("/p/{home_encoded}", Path(home)),
                                 Path("/p/" + encoded))

    def test_home_placeholder_expands_verbatim(self):
        home = Path("/srv/fw/cousins/testa/files")
        self.assertEqual(
            config.expand_harness_path("{home}/data/transcripts", home),
            Path("/srv/fw/cousins/testa/files/data/transcripts"))

    def test_template_without_placeholders_is_taken_as_is(self):
        self.assertEqual(
            config.expand_harness_path("/var/lib/harness", Path("/x")),
            Path("/var/lib/harness"))

    def test_a_leading_tilde_is_the_user_home(self):
        # The harness keeps its state under the user's home; a preset
        # must be copyable without editing in the account's path.
        home = Path("/srv/fw/cousins/testa")
        self.assertEqual(
            config.expand_harness_path("~/.h/projects/{home_encoded}", home),
            Path.home() / ".h" / "projects" / "-srv-fw-cousins-testa")

    def test_the_keys_2_0_0_removed_are_inert_whatever_their_value(self):
        # attention_patterns and flip_when_transcript_mb: named by
        # removed_keys, never read, never a refusal (configuration.md)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            path = Path(root) / "config" / "harness.toml"
            for body in ('attention_patterns = "Select login method"\n',
                         'attention_patterns = [1, ""]\n',
                         'flip_when_transcript_mb = "big"\n',
                         "flip_when_transcript_mb = 0\n",
                         "flip_when_transcript_mb = true\n"):
                path.write_text(body + 'transcripts_dir = "/t/{home_encoded}"\n')
                cfg = config.harness_config(Path(root))
                self.assertEqual(cfg["transcripts_dir"], "/t/{home_encoded}", body)
                self.assertNotIn("attention_patterns", cfg, body)
                self.assertNotIn("flip_when_transcript_mb", cfg, body)

    def test_the_shipped_claude_code_preset_parses(self):
        preset = (Path(__file__).resolve().parents[1] / "config"
                  / "harness.toml.claude-code.example")
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "harness.toml").write_text(
                preset.read_text())
            cfg = config.harness_config(Path(root))
            self.assertTrue(cfg["transcripts_dir"])
            self.assertTrue(cfg["auto_memory_dir"])
            self.assertTrue(cfg["settings_file"])

    def test_unparsable_file_is_loud(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "harness.toml").write_text("not = [toml")
            with self.assertRaises(config.MissingConfigError):
                config.harness_config(Path(root))

    def test_host_label_is_a_non_empty_string_or_loud(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            path = Path(root) / "config" / "harness.toml"
            path.write_text('host_label = "rack-2"\n')
            self.assertEqual(config.harness_config(Path(root))["host_label"], "rack-2")
            for bad in ("host_label = 7\n", 'host_label = "  "\n'):
                path.write_text(bad)
                with self.assertRaises(config.MissingConfigError):
                    config.harness_config(Path(root))


if __name__ == "__main__":
    unittest.main()


class AgentDefaultsSeam(unittest.TestCase):
    """config/harness.toml [agent]: the install-wide model and effort
    the agent-cmd placeholders fall back to when a cousin sets none,
    and the model catalogue the console's spawn dialog offers. Absent
    file or table: no default model, no default effort, the built-in
    catalogue. A default_effort outside the levels is loud."""

    def _root(self, text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if text is not None:
            (root / "config").mkdir()
            (root / "config" / "harness.toml").write_text(text)
        return root

    def test_absent_file_means_no_defaults_and_the_builtin_catalogue(self):
        cfg = config.agent_config(self._root())
        self.assertEqual(set(cfg), {"default_model", "default_effort",
                                    "models"})
        self.assertIsNone(cfg["default_model"])
        self.assertIsNone(cfg["default_effort"])
        self.assertEqual(cfg["models"], list(config.DEFAULT_MODELS))
        self.assertTrue(cfg["models"])

    def test_absent_table_reads_the_same_as_absent_file(self):
        cfg = config.agent_config(self._root(
            'transcripts_dir = "/tmp/h/{home_encoded}"\n'))
        self.assertIsNone(cfg["default_model"])
        self.assertEqual(cfg["models"], list(config.DEFAULT_MODELS))

    def test_reads_the_agent_table(self):
        cfg = config.agent_config(self._root(
            '[agent]\ndefault_model = "m-one"\ndefault_effort = "medium"\n'
            'models = ["m-one", "m-two"]\n'))
        self.assertEqual(cfg, {"default_model": "m-one",
                               "default_effort": "medium",
                               "models": ["m-one", "m-two"]})

    def test_framework_config_exposes_it(self):
        root = self._root('[agent]\ndefault_model = "m-one"\n')
        self.assertEqual(
            config.FrameworkConfig(root).agent_defaults()["default_model"],
            "m-one")

    def test_bad_default_effort_or_models_is_loud(self):
        with self.assertRaises(config.MissingConfigError):
            config.agent_config(self._root(
                '[agent]\ndefault_effort = "extreme"\n'))
        with self.assertRaises(config.MissingConfigError):
            config.agent_config(self._root('[agent]\nmodels = "m-one"\n'))
        with self.assertRaises(config.MissingConfigError):
            config.agent_config(self._root('[agent]\nmodels = [1, 2]\n'))


class CommitAttributionSeam(unittest.TestCase):
    """config/harness.toml [agent] commit_attribution, overridden by a
    cousin's own cousin.toml [agent] commit_attribution:
    whether a commit or PR this cousin makes carries Claude Code's own
    injected attribution. Unset anywhere: True, the CLI's stock
    behaviour - the framework is public and does not impose one
    operator's policy on every install."""

    def _root(self, text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if text is not None:
            (root / "config").mkdir()
            (root / "config" / "harness.toml").write_text(text)
        return root

    def test_unset_anywhere_is_true(self):
        self.assertIs(config.commit_attribution(self._root()), True)

    def test_install_default_false(self):
        root = self._root('[agent]\ncommit_attribution = false\n')
        self.assertIs(config.commit_attribution(root), False)

    def test_install_default_true_is_explicit(self):
        root = self._root('[agent]\ncommit_attribution = true\n')
        self.assertIs(config.commit_attribution(root), True)

    def test_cousin_override_wins_over_install_default(self):
        root = self._root('[agent]\ncommit_attribution = false\n')
        self.assertIs(
            config.commit_attribution(root, {"commit_attribution": True}), True)
        root = self._root('[agent]\ncommit_attribution = true\n')
        self.assertIs(
            config.commit_attribution(root, {"commit_attribution": False}), False)

    def test_cousin_table_with_no_key_falls_back_to_install(self):
        root = self._root('[agent]\ncommit_attribution = false\n')
        self.assertIs(config.commit_attribution(root, {"runner": "sdk"}), False)

    def test_a_non_boolean_install_value_is_loud_not_coerced(self):
        # "false" the string is truthy under bool(); it must never win
        for text in ('[agent]\ncommit_attribution = "false"\n',
                    '[agent]\ncommit_attribution = 0\n',
                    '[agent]\ncommit_attribution = 1\n',
                    '[agent]\ncommit_attribution = ["false"]\n',
                    '[agent]\ncommit_attribution = {x = 1}\n'):
            root = self._root(text)
            with self.assertRaises(config.MissingConfigError):
                config.commit_attribution(root)

    def test_a_non_boolean_cousin_value_is_loud_not_coerced(self):
        root = self._root()
        for bad in ("false", 0, 1, ["false"], {"x": 1}):
            with self.assertRaises(config.MissingConfigError):
                config.commit_attribution(root, {"commit_attribution": bad})

    def test_cousin_type_error_names_cousin_toml_not_the_install_file(self):
        root = self._root('[agent]\ncommit_attribution = false\n')
        with self.assertRaises(config.MissingConfigError) as cm:
            config.commit_attribution(root, {"commit_attribution": "nope"})
        self.assertIn("cousin.toml", str(cm.exception))

    def test_install_type_error_names_harness_toml(self):
        root = self._root('[agent]\ncommit_attribution = "nope"\n')
        with self.assertRaises(config.MissingConfigError) as cm:
            config.commit_attribution(root)
        self.assertIn("config/harness.toml", str(cm.exception))


class TestClaudeCodePreset(unittest.TestCase):
    """The shipped Claude Code preset (config/harness.toml.claude-code.example).

    Canaries from a clean-machine install: the attention flag must stay
    set when the login screen turns into an OAuth error, and a pinned
    `models` list must not hide the current model family."""

    def _preset(self):
        import tomllib
        here = Path(__file__).resolve().parent.parent
        return tomllib.loads(
            (here / "config" / "harness.toml.claude-code.example").read_text())

    def test_the_presets_carry_no_key_2_0_0_removed(self):
        # the legacy pane's attention and busy patterns, [input_mode],
        # the size guard, [agent.resume] and [auth.api_key] are gone; the
        # tmux kind reads its own screen (tmux_pane.attention_in)
        import tomllib
        from cousin_lib import removed_keys
        here = Path(__file__).resolve().parent.parent / "config"
        self.assertEqual(removed_keys.findings(self._preset(), removed_keys.HARNESS_KEYS,
                                               "config/harness.toml"), [])
        commented = tomllib.loads("\n".join(
            line[1:] for line in (here / "harness.toml.example").read_text().splitlines()
            if len(line) > 1 and line.startswith("#") and not line.startswith("# ")))
        self.assertTrue(commented.get("transcripts_dir"))      # the uncommenting worked
        self.assertEqual(removed_keys.findings(commented, removed_keys.HARNESS_KEYS,
                                               "config/harness.toml"), [])

    def test_both_examples_show_every_documented_key_with_its_default(self):
        # configuration.md's harness.toml tables: every key is in both
        # examples (set, or commented out), a default shown at its value
        import tomllib
        here = Path(__file__).resolve().parent.parent / "config"
        top = {"transcripts_dir", "auto_memory_dir", "mcp_logs_dir", "default_flip_at",
               "settings_file", "host_label"}
        agent = {"default_model", "default_effort", "models", "commit_attribution"}
        for name in ("harness.toml.example", "harness.toml.claude-code.example"):
            lines = (here / name).read_text().splitlines()
            shown = tomllib.loads("\n".join(
                line[1:] if len(line) > 1 and line.startswith("#") and not line.startswith("# ")
                else line for line in lines if not line.startswith("# ") and line != "#"))
            self.assertEqual(top - set(shown), set(), name)
            self.assertEqual(agent - set(shown.get("agent", {})), set(), name)
            self.assertEqual(shown["default_flip_at"], "04:00", name)
            self.assertEqual(shown["mcp_logs_dir"],
                             "~/.cache/claude-cli-nodejs/{home_encoded}/mcp-logs-{server}", name)
            self.assertIs(shown["agent"]["commit_attribution"], True, name)

    def test_the_preset_does_not_pin_a_model_catalogue(self):
        self.assertNotIn("models", self._preset().get("agent", {}))
