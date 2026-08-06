"""cousin-spawn: port allocation, creation sequence, cleanup contract.

Tested against real temporary framework roots; nothing is mocked below
the CLI's own seams.
"""
import pathlib
import shutil
import socket
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib.spawn import (
    SpawnError,
    allocate_port,
    create_cousin,
    spawn_main,
    start_cousin,
)
from tests.server.test_injection import _FAKE_TMUX

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class SpawnCase(unittest.TestCase):
    def _root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name)

    def _claim(self, root, slug, port):
        home = root / "cousins" / slug
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\n[chat]\nport = %d\n' % (slug, port)
        )


class TestAllocatePort(SpawnCase):
    # The claimed-set tests inject a never-live predicate: the host
    # running this suite has its own services, and which ports THEY
    # occupy must not decide whether these tests pass.
    def test_first_free_port_in_range_skipping_claimed(self):
        root = self._root()
        self._claim(root, "a", 8090)
        self._claim(root, "b", 8091)
        got = allocate_port(root, start=8090, end=8200,
                            is_live=lambda p: False)
        self.assertEqual(got, 8092)

    def test_claimed_ports_outside_the_scan_range_stay_excluded(self):
        # The scan range decides where to look; the claimed set decides
        # what to skip. A port hand-configured outside today's range
        # must not become allocatable when someone widens the range.
        root = self._root()
        self._claim(root, "a", 9999)
        never = lambda p: False
        self.assertEqual(
            allocate_port(root, start=9998, end=10000, is_live=never), 9998
        )
        self._claim(root, "b", 9998)
        self.assertEqual(
            allocate_port(root, start=9998, end=10000, is_live=never), 10000
        )

    def test_live_bound_port_is_skipped(self):
        root = self._root()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        live = sock.getsockname()[1]
        self.assertEqual(
            allocate_port(root, start=live, end=live + 1), live + 1
        )

    def test_exhaustion_is_an_error_never_a_sentinel(self):
        root = self._root()
        self._claim(root, "a", 9998)
        self._claim(root, "b", 9999)
        with self.assertRaises(SpawnError):
            allocate_port(root, start=9998, end=9999,
                          is_live=lambda p: False)


class CreateCase(SpawnCase):
    def _framework_root(self):
        root = self._root()
        (root / "templates").mkdir()
        shutil.copy(
            _REPO_ROOT / "templates" / "cousin-CLAUDE.template.md",
            root / "templates" / "cousin-CLAUDE.template.md",
        )
        return root

    def _create(self, root, **kw):
        args = dict(slug="wren", name="Wren", role="example cousin",
                    voice="Plain and helpful.", port=8100)
        args.update(kw)
        return create_cousin(root, **args)


class TestCreateCousin(CreateCase):
    def test_creates_the_full_home(self):
        root = self._framework_root()
        out = self._create(root)
        home = root / "cousins" / "wren"
        self.assertEqual(out["home"], home)
        for sub in ("memory", "data", "notes", "scripts"):
            self.assertTrue((home / sub).is_dir(), sub)
        cfg = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(cfg["cousin"]["slug"], "wren")
        self.assertEqual(cfg["chat"]["port"], 8100)
        self.assertEqual(cfg["chat"]["tmux_session"], "wren")
        claude_md = (home / "CLAUDE.md").read_text()
        self.assertIn("# Wren", claude_md)
        self.assertIn("## Voice", claude_md)
        self.assertNotIn("{{", claude_md)
        status = (home / "STATUS.md").read_text()
        # The Open-loops section is the seam the session-end baseline
        # derivation reads; spawning it empty makes the convention real
        # from birth instead of hoping cousins invent it.
        self.assertIn("## Open loops", status)
        self.assertTrue((home / "MEMORY.md").is_file())

    def test_bad_slug_is_rejected_before_anything_is_written(self):
        root = self._framework_root()
        for slug in ("Wren", "1wren", "w" * 40, "wr en", ""):
            with self.assertRaises(SpawnError):
                self._create(root, slug=slug)
        self.assertFalse((root / "cousins").exists())

    def test_existing_cousin_is_a_collision(self):
        root = self._framework_root()
        self._create(root)
        with self.assertRaises(SpawnError):
            self._create(root, port=8101)

    def test_orphan_directory_is_reported_with_its_path(self):
        # A directory without cousin.toml is not a cousin; naming its
        # path is what lets the operator clean it up instead of
        # wondering why the slug is taken.
        root = self._framework_root()
        (root / "cousins" / "wren").mkdir(parents=True)
        with self.assertRaises(SpawnError) as ctx:
            self._create(root)
        self.assertIn("orphan", str(ctx.exception))
        self.assertIn(str(root / "cousins" / "wren"), str(ctx.exception))

    def test_missing_voice_fails_the_render_check_writing_nothing(self):
        root = self._framework_root()
        with self.assertRaises(SpawnError) as ctx:
            self._create(root, voice=None)
        self.assertIn("VOICE_GUIDE", str(ctx.exception))
        self.assertFalse((root / "cousins").exists())

    def test_failure_after_toml_write_removes_the_whole_home(self):
        # The partial state that squats a slug in practice: cousin.toml
        # written, a later step fails. Cleanup must cover it.
        root = self._framework_root()
        with mock.patch(
            "cousin_lib.spawn._write_identity_files",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(SpawnError):
                self._create(root)
        self.assertFalse((root / "cousins" / "wren").exists())

    def test_port_is_allocated_when_not_given(self):
        root = self._framework_root()
        self._claim(root, "a", 8090)
        out = self._create(root, port=None,
                           _is_live=lambda p: False)
        self.assertEqual(out["port"], 8091)


class TestStartCousin(CreateCase):
    def setUp(self):
        super().setUp()
        self.calls = []

    def _fake_tmux(self, root):
        import os as _os
        import stat as _stat
        tmux = root / "tmux"
        tmux.write_text(_FAKE_TMUX)
        tmux.chmod(tmux.stat().st_mode | _stat.S_IEXEC)
        log = root / "tmux-calls.log"
        patcher = mock.patch.dict(
            "os.environ",
            {"FAKE_TMUX_LOG": str(log), "FAKE_TMUX_PANE": str(root / "p")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return tmux, log

    def test_creates_the_session_and_starts_the_chat_server(self):
        root = self._framework_root()
        out = self._create(root)
        tmux, log = self._fake_tmux(root)
        start_cousin(
            out["home"], agent_cmd="my-agent --flag",
            tmux_bin=str(tmux),
            start_chat_server=lambda home: self.calls.append(home),
        )
        text = log.read_text()
        self.assertIn("new-session", text)
        self.assertIn("-s wren", text)
        self.assertIn("my-agent --flag", text)
        self.assertEqual(self.calls, [out["home"]])

    def test_tmux_failure_is_a_spawn_error(self):
        root = self._framework_root()
        out = self._create(root)
        tmux, _ = self._fake_tmux(root)
        with mock.patch.dict("os.environ", {"FAKE_TMUX_RC": "1"}):
            with self.assertRaises(SpawnError):
                start_cousin(out["home"], agent_cmd="my-agent",
                             tmux_bin=str(tmux),
                             start_chat_server=lambda home: None)
        # A start failure keeps the home: restartable, not an orphan.
        self.assertTrue((out["home"] / "cousin.toml").is_file())


class TestSpawnMain(CreateCase):
    def _main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_create_only_exits_zero(self):
        root = self._framework_root()
        rc, out, _ = self._main([
            "wren", "--root", str(root), "--role", "example cousin",
            "--voice", "Plain and helpful.", "--port", "8100",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("chat port 8100", out)
        self.assertTrue(
            (root / "cousins" / "wren" / "cousin.toml").is_file()
        )

    def test_validation_failure_exits_two(self):
        root = self._framework_root()
        rc, _, err = self._main([
            "Wren", "--root", str(root), "--role", "x",
            "--voice", "v",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("invalid slug", err)
        self.assertFalse((root / "cousins").exists())


if __name__ == "__main__":
    unittest.main()
