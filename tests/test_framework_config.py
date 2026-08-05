"""Framework-level configuration: the filesystem is the registry.

The set of cousins on an install is defined by cousins/<slug>/cousin.toml
files under the framework root - never by a service that must be running.
"""
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.config import FrameworkConfig, MissingConfigError


class TestFrameworkConfig(unittest.TestCase):
    def _root(self, cousins):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for slug, toml_text in cousins.items():
            d = root / "cousins" / slug
            d.mkdir(parents=True)
            if toml_text is not None:
                (d / "cousin.toml").write_text(toml_text)
        return root

    def test_missing_root_env_fails_loud(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(MissingConfigError):
                FrameworkConfig.from_env()

    def test_lists_cousins_from_the_filesystem(self):
        root = self._root(
            {
                "wren": '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n',
                "toki": '[cousin]\nslug = "toki"\n[chat]\nport = 8101\nhost = "birdhouse"\n',
            }
        )
        fw = FrameworkConfig(root)
        rows = fw.list_cousins()
        self.assertEqual(
            [(c.slug, c.chat_port, c.chat_host) for c in rows],
            [("toki", 8101, "birdhouse"), ("wren", 8100, None)],
        )

    def test_directory_without_cousin_toml_is_not_a_cousin(self):
        root = self._root({"wren": '[cousin]\nslug = "wren"\n', "junk": None})
        self.assertEqual([c.slug for c in FrameworkConfig(root).list_cousins()], ["wren"])

    def test_peer_visible_defaults_true_and_reads_config(self):
        root = self._root(
            {
                "wren": '[cousin]\nslug = "wren"\n',
                "quiet": '[cousin]\nslug = "quiet"\npeer_visible = false\n',
            }
        )
        rows = {c.slug: c.peer_visible for c in FrameworkConfig(root).list_cousins()}
        self.assertTrue(rows["wren"])
        self.assertFalse(rows["quiet"])


if __name__ == "__main__":
    unittest.main()
