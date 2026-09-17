"""Configuration seam.

Every hardcoded root, port, and personal default in the source framework
becomes a lookup here. Fail-loud rule: a missing COUSIN_HOME is an error
with a message, never a silent fallback to somebody's home directory.
"""
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.config import CousinConfig, MissingConfigError


class TestCousinConfig(unittest.TestCase):
    def _home(self, toml_text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        if toml_text is not None:
            (home / "cousin.toml").write_text(toml_text)
        return home

    def test_loads_identity_and_chat_port_from_cousin_toml(self):
        home = self._home(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[chat]\nport = 8100\n'
        )
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.slug, "wren")
        self.assertEqual(cfg.name, "Wren")
        self.assertEqual(cfg.chat_port, 8100)

    def test_missing_cousin_home_env_fails_loud(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(MissingConfigError):
                CousinConfig.from_env()

    def test_from_env_reads_cousin_home(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        with mock.patch.dict("os.environ", {"COUSIN_HOME": str(home)}):
            cfg = CousinConfig.from_env()
        self.assertEqual(cfg.slug, "wren")

    def test_operator_name_defaults_to_none_not_a_person(self):
        # The null-operator profile starts here: no configured operator
        # means no operator, never a default human.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        cfg = CousinConfig.load(home)
        self.assertIsNone(cfg.operator_name)

    def test_operator_name_from_config_when_present(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[operator]\nname = "Sam"\n'
        )
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.operator_name, "Sam")

    def test_tmux_session_defaults_to_slug(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.tmux_session, "wren")

    def test_tmux_session_from_config_when_present(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            'tmux_session = "wren-main"\n'
        )
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.tmux_session, "wren-main")

    def test_memory_scope_defaults_to_private(self):
        # The privacy gate's deny-on-uncertainty starts here: unset
        # means private, never shared.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).memory_scope, "private")

    def test_memory_scope_from_config(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[memory]\nscope = "both"\n'
        )
        self.assertEqual(CousinConfig.load(home).memory_scope, "both")

    def test_proactive_recall_defaults_on(self):
        # A colleague remembers without being asked unless told not to.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertIs(CousinConfig.load(home).proactive_recall, True)

    def test_proactive_recall_explicit_off(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[memory]\nproactive_recall = false\n'
        )
        self.assertIs(CousinConfig.load(home).proactive_recall, False)

    def test_heartbeat_default_is_one_value_everywhere(self):
        # The source shipped three different beat defaults across
        # code, template, and docs. One value, stated in the spec.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).heartbeat_seconds, 3600)

    def test_heartbeat_from_config(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[heartbeat]\ncontext_beat_seconds = 600\n')
        self.assertEqual(CousinConfig.load(home).heartbeat_seconds, 600)

    def test_cousin_type_defaults_to_cousin(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).type, "cousin")

    def test_worker_type_from_config(self):
        home = self._home(
            '[cousin]\nslug = "g"\ntype = "worker"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).type, "worker")

    def test_flip_at_defaults_to_none_and_parses(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertIsNone(CousinConfig.load(home).flip_at)
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[lifecycle]\nflip_at = "04:00"\n')
        self.assertEqual(CousinConfig.load(home).flip_at, "04:00")

    def test_missing_chat_port_fails_loud(self):
        home = self._home('[cousin]\nslug = "wren"\n')
        cfg = CousinConfig.load(home)
        with self.assertRaises(MissingConfigError):
            cfg.require_chat_port()


if __name__ == "__main__":
    unittest.main()


class RecallKeywordOnlyKey(unittest.TestCase):
    def test_default_false_and_explicit_true(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
            self.assertFalse(CousinConfig.load(home).recall_keyword_only)
            (home / "cousin.toml").write_text(
                '[cousin]\nslug = "wren"\nname = "Wren"\n[memory]\nrecall_keyword_only = true\n')
            self.assertTrue(CousinConfig.load(home).recall_keyword_only)


class TestRuntimeModelAndEffort(unittest.TestCase):
    """cousin.toml [runtime] model and effort: per-cousin values the
    agent-cmd placeholders {model} and {effort} render from. Absent is
    None (the install default applies); an effort outside the four
    levels is loud, because a misspelt level would otherwise reach the
    agent binary as a flag it rejects at the far end of a spawn."""

    def _home(self, toml_text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        (home / "cousin.toml").write_text(toml_text)
        return home

    def test_absent_means_none_never_a_vendor_default(self):
        cfg = CousinConfig.load(
            self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'))
        self.assertIsNone(cfg.model)
        self.assertIsNone(cfg.effort)

    def test_reads_both_from_runtime(self):
        cfg = CousinConfig.load(self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[runtime]\nmodel = "some-model"\neffort = "low"\n'))
        self.assertEqual(cfg.model, "some-model")
        self.assertEqual(cfg.effort, "low")

    def test_effort_outside_the_levels_is_loud(self):
        from cousin_lib.config import EFFORT_LEVELS
        self.assertEqual(EFFORT_LEVELS, ("low", "medium", "high", "max"))
        with self.assertRaises(MissingConfigError) as ctx:
            CousinConfig.load(self._home(
                '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
                '[runtime]\neffort = "xhigh"\n'))
        self.assertIn("effort", str(ctx.exception))
        self.assertIn("xhigh", str(ctx.exception))
