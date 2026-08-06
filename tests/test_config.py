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

    def test_missing_chat_port_fails_loud(self):
        home = self._home('[cousin]\nslug = "wren"\n')
        cfg = CousinConfig.load(home)
        with self.assertRaises(MissingConfigError):
            cfg.require_chat_port()


if __name__ == "__main__":
    unittest.main()
