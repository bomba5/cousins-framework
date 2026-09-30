"""Server assembly: real seams wired, startup failing loud."""
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from cousin_lib.server.app import StartupError, build_server
from tests._fakes import _FAKE_TMUX


class AssemblyCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.home = root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[chat]\nport = 0\n'
        )
        self.tmux = root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.log = root / "calls.log"
        self.pane = root / "pane.txt"
        self.pane.write_text("")
        patcher = mock.patch.dict(os.environ, {
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(self.pane),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_guard_is_wired_with_defaults(self):
        server = build_server(self.home, tmux_bin=str(self.tmux))
        self.assertTrue(server.guard("127.0.0.1"))
        self.assertFalse(server.guard("8.8.8.8"))

    def test_absent_tmux_binary_is_a_startup_error(self):
        with self.assertRaises(StartupError):
            build_server(self.home, tmux_bin="/nonexistent/tmux")

    def test_no_terminal_delivery_needs_no_tmux(self):
        server = build_server(self.home, tmux_bin="/nonexistent/tmux",
                              terminal_delivery=False)
        self.assertIsNone(server.deliver)


if __name__ == "__main__":
    unittest.main()
