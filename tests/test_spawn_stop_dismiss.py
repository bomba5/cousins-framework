"""spawn.stop_cousin and spawn.dismiss_cousin: the console's stop and
delete paths, usable with no console running.

Stop refuses a cousin with no runner kind by name (R2) and is a no-op
that says so for a worker (R14); a runner cousin's stop is the
supervisor's. Dismiss archives the whole home before removing it and
REFUSES the delete when the archive cannot be written: the archive is
the only copy of never-tracked notes, so a delete without it is a
silent loss.
"""
import os
import pathlib
import re
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
        # stop_cousin falls back to "whatever chat server holds the
        # cousin's port on this host". With fixture ports that is some
        # other process on the machine running the suite (another
        # checkout's test server, or a live cousin), and the fallback
        # would SIGTERM it. A test that wants the fallback passes its
        # own port_pid.
        kw = mock.patch.dict(stop_cousin.__kwdefaults__,
                             {"port_pid": lambda port: None})
        kw.start()
        self.addCleanup(kw.stop)

    runner = None

    def _cousin(self, slug="wren", port=8100):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "Wren"\n[chat]\nport = %d\n'
            % (slug, port)
            + ('[agent]\nrunner = "%s"\n' % self.runner if self.runner else ""))
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


class TestLegacyStopRefused(StopCase):
    """R2, R14: stop_cousin refuses a cousin with no [agent] runner by
    name before any tmux call; a worker has no session, so its stop is a
    no-op that says so."""

    def test_stopping_a_cousin_with_no_runner_is_refused_and_runs_no_tmux(self):
        from cousin_lib.delivery import lane_refusal
        home = self._cousin()
        with self.assertRaises(SpawnError) as ctx:
            stop_cousin(home, tmux_bin=str(self.tmux))
        self.assertEqual(str(ctx.exception), lane_refusal(home))
        self.assertFalse(self.log.exists() and self.log.read_text())

    def test_stopping_a_worker_is_a_no_op_that_says_so(self):
        from cousin_lib.delivery import lane_refusal
        home = self._cousin()
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\ntype = "worker"\n')
        out = stop_cousin(home, tmux_bin=str(self.tmux))
        self.assertEqual(out, {"worker": "no session", "note": lane_refusal(home)})
        self.assertIn("worker", out["note"])
        self.assertFalse(self.log.exists() and self.log.read_text())


class TestDismissCousin(StopCase):
    runner = "fake"   # a runner cousin: its stop is the supervisor's (R2)
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
        self.assertIn(re.sub(r"[^A-Za-z0-9]", "-", str(home)), out["left_in_place"][0])


if __name__ == "__main__":
    unittest.main()
