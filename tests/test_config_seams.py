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
                                        "settings_file"})
            self.assertIsNone(cfg["auto_memory_dir"])
            self.assertIsNone(cfg["settings_file"])

    def test_home_placeholder_expands_verbatim(self):
        home = Path("/srv/fw/cousins/testa/files")
        self.assertEqual(
            config.expand_harness_path("{home}/data/transcripts", home),
            Path("/srv/fw/cousins/testa/files/data/transcripts"))

    def test_template_without_placeholders_is_taken_as_is(self):
        self.assertEqual(
            config.expand_harness_path("/var/lib/harness", Path("/x")),
            Path("/var/lib/harness"))

    def test_unparsable_file_is_loud(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "harness.toml").write_text("not = [toml")
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
