"""Server assembly: real seams wired, startup failing loud.

The end-to-end test here is the unit's receipt: a real POST from
loopback lands in a (fake) tmux pane through the whole stack.
"""
import io
import json
import os
import pathlib
import stat
import tempfile
import time
import unittest
import urllib.request
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

    def test_send_reaches_the_pane_through_the_whole_stack(self):
        server = build_server(self.home, tmux_bin=str(self.tmux))
        server.start()
        self.addCleanup(server.stop)
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/send" % server.port,
            data=json.dumps({"user": "Sam", "message": "hello"}).encode(),
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        for _ in range(500):
            if self.log.exists() and " -l " in self.log.read_text():
                break
            time.sleep(0.02)
        else:
            self.fail("delivery never reached the fake tmux")
        paste = next(c for c in self.log.read_text().splitlines()
                     if " -l " in c)
        self.assertIn("(Chat Sam): hello", paste)
        self.assertIn("send-keys -t wren -l", paste)


    def test_menu_pane_is_skipped_with_the_root_taken_from_the_home(self):
        # spawn starts the chat server with --home only; the attention
        # guard still reads the install's harness.toml, via the home.
        root = self.home.parent.parent
        (root / "config").mkdir()
        (root / "config" / "harness.toml").write_text(
            'attention_patterns = ["Select login method"]\n')
        self.pane.write_text("Select login method:\n 1. Account\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            server = build_server(self.home, tmux_bin=str(self.tmux))
            server.start()
            self.addCleanup(server.stop)
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/send" % server.port,
                data=json.dumps({"user": "Sam", "message": "2"}).encode(),
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            for _ in range(500):
                if self.log.exists() and "capture-pane" in self.log.read_text():
                    break
                time.sleep(0.02)
            else:
                self.fail("the guard never read the pane")
            time.sleep(0.2)
        self.assertNotIn("send-keys", self.log.read_text())
        self.assertIn("SKIPPED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
