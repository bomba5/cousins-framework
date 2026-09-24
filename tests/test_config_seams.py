"""Config seams added in phase 0: config/harness.toml.

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
                                        "flip_when_transcript_mb",
                                        "settings_file",
                                        "attention_patterns", "host_label"})
            self.assertEqual(cfg["attention_patterns"], [])
            self.assertIsNone(cfg["host_label"])
            self.assertIsNone(cfg["auto_memory_dir"])
            self.assertIsNone(cfg["settings_file"])

    def test_home_encoded_is_the_harness_encoding_every_non_alphanumeric_is_a_dash(self):
        """#106: Claude Code names a project dir by its path with every
        character that is not a letter or a digit turned into '-' (seen on
        this host: /tmp/tmpxpnoz7t_/cousins/wren is -tmp-tmpxpnoz7t--cousins-wren);
        only '/' was mapped, so a home with '.', '_' or another character
        resolved a transcripts dir that does not exist."""
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

    def test_attention_patterns_are_a_list_of_strings_or_loud(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            path = Path(root) / "config" / "harness.toml"
            path.write_text('attention_patterns = ["Select login method"]\n')
            self.assertEqual(config.harness_config(Path(root))
                             ["attention_patterns"], ["Select login method"])
            path.write_text('attention_patterns = "Select login method"\n')
            with self.assertRaises(config.MissingConfigError):
                config.harness_config(Path(root))

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
            self.assertTrue(cfg["attention_patterns"])

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


class TestClaudeCodePreset(unittest.TestCase):
    """The shipped Claude Code preset (config/harness.toml.claude-code.example).

    Canaries from the clean-machine install re-test (2026-09-18): the
    attention flag cleared once the login screen turned into an OAuth
    error, and a pinned `models` list hid the current model family."""

    def _preset(self):
        import tomllib
        here = Path(__file__).resolve().parent.parent
        return tomllib.loads(
            (here / "config" / "harness.toml.claude-code.example").read_text())

    def test_every_blocking_first_run_screen_needs_attention(self):
        patterns = self._preset()["attention_patterns"]
        for screen in ("Select login method", "Paste code here if prompted",
                       "OAuth error", "Press Enter to retry",
                       "Choose the text style", "Yes, I trust this folder",
                       "Do you want to use this API key"):
            self.assertIn(screen, patterns)

    def test_the_preset_does_not_pin_a_model_catalogue(self):
        self.assertNotIn("models", self._preset().get("agent", {}))
