"""spawn.stop_cousin and spawn.dismiss_cousin: the console's stop and
delete paths, usable with no console running.

Stop is idempotent and names what it did to each half (tmux session,
chat server). Dismiss archives the whole home before removing it and
REFUSES the delete when the archive cannot be written: the archive is
the only copy of never-tracked notes, so a delete without it is a
silent loss.
"""
import os
import pathlib
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.spawn import (DismissRefused, SpawnError, dismiss_cousin,
                              stop_cousin)
from tests.console._harness import FAKE_TMUX


class StopCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.tmux = self.root / "tmux"
        self.tmux.write_text(FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.log = self.root / "tmux.log"
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(self.root / "pane"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cousin(self, slug="wren", port=8100):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "Wren"\n[chat]\nport = %d\n'
            % (slug, port))
        (home / "notes").mkdir()
        (home / "notes" / "untracked.md").write_text("only copy\n")
        return home

    def _fake_chat_server(self, home):
        # A process whose cmdline carries the chat-server marker, as the
        # real one does; stop_cousin refuses to kill a pid without it.
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import time  # cousin_lib.server.app\ntime.sleep(30)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        (home / "data" / "chat-server.pid").write_text(str(proc.pid))
        return proc


class TestStopCousin(StopCase):
    def test_kills_the_session_and_the_chat_server_it_spawned(self):
        home = self._cousin()
        proc = self._fake_chat_server(home)
        out = stop_cousin(home, tmux_bin=str(self.tmux))
        self.assertEqual(out, {"tmux": "stopped", "chat_server": "stopped"})
        self.assertIn("kill-session -t wren", self.log.read_text())
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse((home / "data" / "chat-server.pid").exists())

    def test_is_idempotent_and_names_what_was_already_down(self):
        home = self._cousin()
        with mock.patch.dict(os.environ, {"FAKE_TMUX_RC_HAS_SESSION": "1"}):
            out = stop_cousin(home, tmux_bin=str(self.tmux))
        self.assertEqual(out, {"tmux": "already stopped",
                               "chat_server": "not running"})
        self.assertNotIn("kill-session", self.log.read_text())

    def test_a_stale_pid_pointing_at_another_program_is_not_killed(self):
        home = self._cousin()
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        (home / "data" / "chat-server.pid").write_text(str(proc.pid))
        out = stop_cousin(home, tmux_bin=str(self.tmux),
                          port_pid=lambda port: None)
        self.assertEqual(out["chat_server"], "not running")
        self.assertIsNone(proc.poll())

    def test_falls_back_to_the_process_bound_to_the_port(self):
        home = self._cousin()
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import time  # cousin-chat-server\ntime.sleep(30)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        out = stop_cousin(home, tmux_bin=str(self.tmux),
                          port_pid=lambda port: proc.pid if port == 8100
                          else None)
        self.assertEqual(out["chat_server"], "stopped")
        proc.wait(timeout=5)


class TestDismissCousin(StopCase):
    def test_archives_the_whole_home_then_removes_it(self):
        home = self._cousin()
        out = dismiss_cousin(self.root, slug="wren", tmux_bin=str(self.tmux))
        self.assertEqual(out["status"], "deleted")
        self.assertFalse(home.exists())
        archive = pathlib.Path(out["archive"])
        self.assertTrue(archive.is_relative_to(self.root / "data"
                                               / "dismissed"))
        self.assertTrue(archive.name.startswith("wren-"))
        with tarfile.open(archive) as tf:
            names = tf.getnames()
        self.assertIn("wren/notes/untracked.md", names)
        self.assertIn("wren/cousin.toml", names)
        self.assertEqual(out["left_in_place"], [])
        # Stopped first: the session was killed through the fake tmux.
        self.assertIn("kill-session -t wren", self.log.read_text())

    def test_refuses_when_the_archive_cannot_be_written(self):
        home = self._cousin()
        (self.root / "data").mkdir()
        (self.root / "data" / "dismissed").write_text("a file, not a dir")
        with self.assertRaises(DismissRefused) as ctx:
            dismiss_cousin(self.root, slug="wren", tmux_bin=str(self.tmux))
        self.assertTrue(str(ctx.exception).startswith("refusing to delete"))
        self.assertTrue((home / "notes" / "untracked.md").is_file())

    def test_refuses_when_the_archive_dir_is_inside_the_tree(self):
        home = self._cousin()
        (home / "arch").mkdir()
        (self.root / "data").symlink_to(home / "arch")
        with self.assertRaises(DismissRefused) as ctx:
            dismiss_cousin(self.root, slug="wren", tmux_bin=str(self.tmux))
        self.assertIn("inside", str(ctx.exception))
        self.assertTrue(home.exists())

    def test_unknown_cousin_is_a_spawn_error(self):
        with self.assertRaises(SpawnError):
            dismiss_cousin(self.root, slug="nobody", tmux_bin=str(self.tmux))

    def test_names_harness_directories_it_left_in_place(self):
        home = self._cousin()
        (self.root / "config" / "harness.toml").write_text(
            'transcripts_dir = "/nonexistent/projects/{home_encoded}"\n')
        out = dismiss_cousin(self.root, slug="wren", tmux_bin=str(self.tmux))
        self.assertEqual(len(out["left_in_place"]), 1)
        self.assertIn(str(home).replace("/", "-"), out["left_in_place"][0])


if __name__ == "__main__":
    unittest.main()
