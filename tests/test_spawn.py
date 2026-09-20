"""cousin-spawn: port allocation, creation sequence, cleanup contract.

Tested against real temporary framework roots; nothing is mocked below
the CLI's own seams.
"""
import json
import pathlib
import shutil
import socket
import tempfile
import tomllib
import unittest
from pathlib import Path
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

    def test_start_distills_raw_memory_first(self):
        # Only a flip assembled a boot packet, so a cousin that was only
        # ever started or resumed never had its distilled views built.
        from cousin_lib import memory
        root = self._framework_root()
        out = self._create(root)
        tmux, _ = self._fake_tmux(root)
        memory._append_raw(out["home"], {
            "topic": "retention window",
            "content": "keep thirty days of flows",
            "truth_level": "cousin-conclusion", "source": "decision"})
        start_cousin(out["home"], agent_cmd="my-agent",
                     tmux_bin=str(tmux), start_chat_server=lambda h: None)
        text = (out["home"] / "memory" / "distilled"
                / "decisions.md").read_text()
        self.assertIn("keep thirty days of flows", text)

    def test_a_failing_distill_never_stops_a_start(self):
        root = self._framework_root()
        out = self._create(root)
        tmux, log = self._fake_tmux(root)
        with mock.patch("cousin_lib.distill.distill",
                        side_effect=RuntimeError("boom")):
            start_cousin(out["home"], agent_cmd="my-agent",
                         tmux_bin=str(tmux),
                         start_chat_server=lambda h: None)
        self.assertIn("new-session", log.read_text())

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


_STUB_TMUX = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_TMUX_LOG"
case "$1" in
  has-session) exit "${STUB_TMUX_ALIVE_RC:-1}" ;;
esac
exit 0
"""


class TestStartPreflightAndExisting(CreateCase):
    """`cousin-spawn --start` checks what a start needs (tmux, the
    agent command's executable) BEFORE creating anything, and
    `cousin-spawn <slug> --start` starts a cousin that already exists:
    the recovery its own "start failed" message promises."""

    def setUp(self):
        super().setUp()
        import os as _os
        import stat as _stat
        self.root = self._framework_root()
        (self.root / "config").mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "tmux.log"
        self.started = []
        for name, text in (("tmux", _STUB_TMUX),
                           ("my-agent", "#!/bin/sh\nexit 0\n")):
            f = self.bin / name
            f.write_text(text)
            f.chmod(f.stat().st_mode | _stat.S_IEXEC)
        (self.root / "config" / "agent-cmd").write_text("my-agent --x\n")
        patcher = mock.patch.dict(_os.environ, {
            "PATH": str(self.bin), "STUB_TMUX_LOG": str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        chat = mock.patch("cousin_lib.spawn._default_chat_server",
                          side_effect=lambda home: self.started.append(home))
        chat.start()
        self.addCleanup(chat.stop)

    def _main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _create_argv(self):
        return ["wren", "--root", str(self.root), "--role", "x",
                "--voice", "v", "--port", "8100", "--start"]

    def test_no_tmux_refuses_before_creating_anything(self):
        (self.bin / "tmux").unlink()
        rc, _out, err = self._main(self._create_argv())
        self.assertEqual(rc, 2)
        self.assertIn("tmux", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse((self.root / "cousins").exists())

    def test_unresolvable_agent_refuses_before_creating_anything(self):
        (self.bin / "my-agent").unlink()
        rc, _out, err = self._main(self._create_argv())
        self.assertEqual(rc, 2)
        self.assertIn("my-agent", err)
        self.assertIn("agent-cmd", err)
        self.assertFalse((self.root / "cousins").exists())

    def test_no_agent_cmd_refuses_before_creating_anything(self):
        (self.root / "config" / "agent-cmd").unlink()
        rc, _out, err = self._main(self._create_argv())
        self.assertEqual(rc, 2)
        self.assertIn("agent-cmd", err)
        self.assertFalse((self.root / "cousins").exists())

    def test_create_and_start_passes_preflight(self):
        rc, out, err = self._main(self._create_argv())
        self.assertEqual(rc, 0, err)
        self.assertIn("started wren", out)
        self.assertIn("new-session", self.log.read_text())

    def test_an_existing_cousin_starts_with_start_alone(self):
        # A port nothing listens on, so the chat server is started.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
        sock.close()
        self._create(self.root, port=free)
        rc, out, err = self._main(["wren", "--root", str(self.root),
                                   "--start"])
        self.assertEqual(rc, 0, err)
        self.assertIn("started wren", out)
        self.assertIn("new-session", self.log.read_text())
        self.assertEqual(self.started, [self.root / "cousins" / "wren"])

    def test_a_chat_server_already_on_the_port_is_not_doubled(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        self._create(self.root, port=sock.getsockname()[1])
        rc, _out, err = self._main(["wren", "--root", str(self.root),
                                    "--start"])
        self.assertEqual(rc, 0, err)
        self.assertIn("new-session", self.log.read_text())
        self.assertEqual(self.started, [])

    def test_an_existing_live_cousin_is_left_alone(self):
        self._create(self.root)
        with mock.patch.dict("os.environ", {"STUB_TMUX_ALIVE_RC": "0"}):
            rc, out, err = self._main(["wren", "--root", str(self.root),
                                       "--start"])
        self.assertEqual(rc, 0, err)
        self.assertIn("already running", out)
        self.assertNotIn("new-session", self.log.read_text())
        self.assertEqual(self.started, [])

    def test_an_existing_cousin_start_checks_tmux_too(self):
        self._create(self.root)
        (self.bin / "tmux").unlink()
        rc, _out, err = self._main(["wren", "--root", str(self.root),
                                    "--start"])
        self.assertEqual(rc, 2)
        self.assertIn("tmux", err)

    def test_create_over_an_existing_cousin_names_the_start_path(self):
        self._create(self.root)
        rc, _out, err = self._main(self._create_argv())
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        self.assertIn("cousin-spawn wren --start", err)

    def test_start_alone_on_an_unknown_slug_still_needs_role_and_voice(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                spawn_main(["ghost", "--root", str(self.root), "--start"])
        self.assertFalse((self.root / "cousins").exists())


if __name__ == "__main__":
    unittest.main()


class TestStartSubstitutesSessionId(TestStartCousin):
    """A plain start (console, cousin-spawn --start) must render the
    {session_id} placeholder exactly as a flip does: a fresh uuid in the
    argv, persisted to cousin.toml [runtime]. Before this, only flip.py
    substituted and a plain start handed the literal braces to the
    agent."""

    def test_placeholder_is_rendered_and_persisted(self):
        import re as _re
        import tomllib as _tomllib
        root = self._framework_root()
        out = self._create(root)
        tmux, log = self._fake_tmux(root)
        start_cousin(
            out["home"], agent_cmd="my-agent --session-id {session_id}",
            tmux_bin=str(tmux), start_chat_server=lambda home: None,
        )
        text = log.read_text()
        self.assertNotIn("{session_id}", text)
        m = _re.search(r"--session-id ([0-9a-f-]{36})", text)
        self.assertIsNotNone(m, text)
        conf = _tomllib.loads((out["home"] / "cousin.toml").read_text())
        self.assertEqual(conf["runtime"]["session_id"], m.group(1))

    def test_without_placeholder_nothing_is_persisted(self):
        import tomllib as _tomllib
        root = self._framework_root()
        out = self._create(root)
        tmux, log = self._fake_tmux(root)
        start_cousin(
            out["home"], agent_cmd="my-agent --flag",
            tmux_bin=str(tmux), start_chat_server=lambda home: None,
        )
        conf = _tomllib.loads((out["home"] / "cousin.toml").read_text())
        self.assertNotIn("session_id", conf.get("runtime", {}))


class TestModelAndEffortPlaceholders(TestStartCousin):
    """{model} and {effort} in the agent-cmd render at the single spawn
    site from cousin.toml [runtime], else config/harness.toml [agent]
    defaults; a placeholder with no value anywhere is a SpawnError that
    names both files, never a guessed vendor default."""

    def _harness(self, root, text):
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "harness.toml").write_text(text)

    def test_cousin_values_win_over_harness_defaults(self):
        root = self._framework_root()
        out = self._create(root, model="own-model", effort="low")
        self._harness(root, '[agent]\ndefault_model = "dm"\n'
                            'default_effort = "high"\n')
        tmux, log = self._fake_tmux(root)
        start_cousin(out["home"], agent_cmd="my-agent --model {model}"
                     " --effort {effort}", tmux_bin=str(tmux), root=root,
                     start_chat_server=lambda home: None)
        text = log.read_text()
        self.assertIn("--model own-model --effort low", text)
        self.assertNotIn("{model}", text)
        self.assertNotIn("{effort}", text)

    def test_harness_defaults_fill_what_the_cousin_leaves_unset(self):
        root = self._framework_root()
        out = self._create(root)
        self._harness(root, '[agent]\ndefault_model = "dm"\n'
                            'default_effort = "medium"\n')
        tmux, log = self._fake_tmux(root)
        start_cousin(out["home"], agent_cmd="my-agent --model {model}"
                     " --effort {effort}", tmux_bin=str(tmux), root=root,
                     start_chat_server=lambda home: None)
        self.assertIn("--model dm --effort medium", log.read_text())

    def test_root_falls_back_to_the_environment(self):
        root = self._framework_root()
        out = self._create(root)
        self._harness(root, '[agent]\ndefault_model = "dm"\n')
        tmux, log = self._fake_tmux(root)
        with mock.patch.dict("os.environ", {"FRAMEWORK_ROOT": str(root)}):
            start_cousin(out["home"], agent_cmd="my-agent --model {model}",
                         tmux_bin=str(tmux),
                         start_chat_server=lambda home: None)
        self.assertIn("--model dm", log.read_text())

    def test_missing_value_is_a_spawn_error_naming_both_files(self):
        root = self._framework_root()
        out = self._create(root)
        tmux, log = self._fake_tmux(root)
        with self.assertRaises(SpawnError) as ctx:
            start_cousin(out["home"], agent_cmd="my-agent --effort {effort}",
                         tmux_bin=str(tmux), root=root,
                         start_chat_server=lambda home: None)
        msg = str(ctx.exception)
        self.assertIn("{effort}", msg)
        self.assertIn(str(out["home"] / "cousin.toml"), msg)
        self.assertIn(str(root / "config" / "harness.toml"), msg)
        self.assertIn("default_effort", msg)
        # Nothing was started: the error came before tmux.
        self.assertFalse(log.exists())

    def test_without_placeholders_no_value_is_needed(self):
        root = self._framework_root()
        out = self._create(root)
        tmux, log = self._fake_tmux(root)
        start_cousin(out["home"], agent_cmd="my-agent", tmux_bin=str(tmux),
                     root=root, start_chat_server=lambda home: None)
        self.assertIn("my-agent", log.read_text())

    def test_render_is_exposed_for_preflight_and_keeps_session_id(self):
        from cousin_lib.spawn import render_agent_cmd
        root = self._framework_root()
        out = self._create(root, model="own-model", effort="max")
        cmd = render_agent_cmd(
            "a --m {model} --e {effort} --s {session_id}", out["home"],
            root=root)
        self.assertEqual(cmd, "a --m own-model --e max --s {session_id}")



class TestPersistIdentityValues(CreateCase):
    """persist_identity sets the three identity keys the console edits
    ([operator] name, [memory] scope, [heartbeat] context_beat_seconds)
    with a targeted edit that keeps comments and every other table, and
    validates first: a refused value leaves the file byte-for-byte as
    it was."""

    def test_sets_each_key_keeping_comments_and_other_tables(self):
        from cousin_lib.config import CousinConfig
        from cousin_lib.spawn import persist_identity
        root = self._framework_root()
        out = self._create(root)
        path = out["home"] / "cousin.toml"
        path.write_text("# hand note\n" + path.read_text()
                        + '\n[runtime]\nsession_id = "abc"  # kept\n')
        persist_identity(out["home"], "operator", "Kestrel")
        persist_identity(out["home"], "memory_scope", "both")
        persist_identity(out["home"], "heartbeat", 7200)
        persist_identity(out["home"], "heartbeat", 600)
        text = path.read_text()
        self.assertTrue(text.startswith("# hand note\n"))
        self.assertIn('session_id = "abc"  # kept', text)
        self.assertEqual(text.count("[heartbeat]"), 1)
        cfg = CousinConfig.load(out["home"])
        self.assertEqual((cfg.operator_name, cfg.memory_scope,
                          cfg.heartbeat_seconds), ("Kestrel", "shared", 600))

    def test_refuses_bad_values_and_leaves_the_file_untouched(self):
        from cousin_lib.spawn import persist_identity
        root = self._framework_root()
        out = self._create(root)
        path = out["home"] / "cousin.toml"
        before = path.read_bytes()
        bad = [("operator", ""), ("operator", "   "), ("operator", "a\nb"),
               ("operator", "x" * 65), ("operator", 3),
               ("memory_scope", "all"), ("memory_scope", None),
               ("heartbeat", 0), ("heartbeat", 59), ("heartbeat", -1),
               ("heartbeat", 30 * 86400 + 1), ("heartbeat", True),
               ("heartbeat", "600"), ("heartbeat", 60.5),
               ("slug", "other")]
        for key, value in bad:
            with self.assertRaises(SpawnError, msg=(key, value)):
                persist_identity(out["home"], key, value)
        self.assertEqual(path.read_bytes(), before)

    def test_bounds_are_inclusive(self):
        from cousin_lib.spawn import (HEARTBEAT_MAX_SECONDS,
                                      HEARTBEAT_MIN_SECONDS,
                                      persist_identity)
        self.assertEqual((HEARTBEAT_MIN_SECONDS, HEARTBEAT_MAX_SECONDS),
                         (60, 30 * 86400))
        root = self._framework_root()
        out = self._create(root)
        persist_identity(out["home"], "heartbeat", 60)
        persist_identity(out["home"], "heartbeat", 30 * 86400)
        self.assertEqual(tomllib.loads((out["home"] / "cousin.toml")
                                       .read_text())["heartbeat"],
                         {"context_beat_seconds": 30 * 86400})


class TestPersistRuntimeValues(CreateCase):
    """persist_runtime mirrors the session-id write: a targeted line
    replace inside [runtime], re-parsed, renamed into place, every other
    line untouched. The values are validated before the write because
    they render into an argv through shlex: a space or a quote in a
    model name would become a second argument."""

    def test_sets_and_replaces_inside_runtime_keeping_the_rest(self):
        from cousin_lib.spawn import persist_runtime
        root = self._framework_root()
        out = self._create(root)
        path = out["home"] / "cousin.toml"
        path.write_text(path.read_text() + '\n[runtime]\nsession_id = "abc"\n'
                        '\n[memory]\nscope = "shared"\n')
        persist_runtime(out["home"], "model", "m-one")
        persist_runtime(out["home"], "effort", "low")
        persist_runtime(out["home"], "model", "m-two")
        data = tomllib.loads(path.read_text())
        self.assertEqual(data["runtime"], {"session_id": "abc",
                                           "model": "m-two", "effort": "low"})
        self.assertEqual(data["memory"]["scope"], "shared")
        self.assertEqual(data["cousin"]["name"], "Wren")
        self.assertEqual(path.read_text().count("[runtime]"), 1)

    def test_creates_the_table_when_absent(self):
        from cousin_lib.spawn import persist_runtime
        root = self._framework_root()
        out = self._create(root)
        persist_runtime(out["home"], "effort", "high")
        data = tomllib.loads((out["home"] / "cousin.toml").read_text())
        self.assertEqual(data["runtime"]["effort"], "high")

    def test_rejects_unsafe_values_and_unknown_levels(self):
        from cousin_lib.spawn import persist_runtime
        root = self._framework_root()
        out = self._create(root)
        for bad in ("two words", 'q"uote', "", "a;b", "$(x)"):
            with self.assertRaises(SpawnError, msg=bad):
                persist_runtime(out["home"], "model", bad)
        with self.assertRaises(SpawnError):
            persist_runtime(out["home"], "effort", "ultra")
        with self.assertRaises(SpawnError):
            persist_runtime(out["home"], "session_id", "not-through-here")
        self.assertNotIn("runtime", tomllib.loads(
            (out["home"] / "cousin.toml").read_text()))


class TestCreateWithRuntimeOptions(CreateCase):
    def test_create_writes_runtime_heartbeat_and_scope(self):
        root = self._framework_root()
        out = self._create(root, model="m-one", effort="medium",
                           heartbeat=600, memory_scope="both")
        data = tomllib.loads((out["home"] / "cousin.toml").read_text())
        self.assertEqual(data["runtime"], {"model": "m-one",
                                           "effort": "medium"})
        self.assertEqual(data["heartbeat"]["context_beat_seconds"], 600)
        self.assertEqual(data["memory"]["scope"], "shared")
        from cousin_lib.config import CousinConfig
        cfg = CousinConfig.load(out["home"])
        self.assertEqual((cfg.model, cfg.effort, cfg.heartbeat_seconds,
                          cfg.memory_scope), ("m-one", "medium", 600, "shared"))

    def test_options_left_out_write_no_table(self):
        root = self._framework_root()
        out = self._create(root)
        data = tomllib.loads((out["home"] / "cousin.toml").read_text())
        for table in ("runtime", "heartbeat", "memory"):
            self.assertNotIn(table, data)

    def test_bad_options_fail_before_anything_is_written(self):
        root = self._framework_root()
        for kw in (dict(effort="ultra"), dict(memory_scope="everyone"),
                   dict(heartbeat=0), dict(heartbeat="soon"),
                   dict(model="two words")):
            with self.assertRaises(SpawnError, msg=kw):
                self._create(root, **kw)
            self.assertFalse((root / "cousins").exists(), kw)


class TestSpawnMainRuntimeFlags(CreateCase):
    def _main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_flags_reach_cousin_toml(self):
        root = self._framework_root()
        rc, _, err = self._main([
            "wren", "--root", str(root), "--role", "x", "--voice", "v",
            "--port", "8100", "--model", "m-one", "--effort", "max",
            "--heartbeat", "900", "--memory-scope", "shared",
        ])
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(
            (root / "cousins" / "wren" / "cousin.toml").read_text())
        self.assertEqual(data["runtime"], {"model": "m-one", "effort": "max"})
        self.assertEqual(data["heartbeat"]["context_beat_seconds"], 900)
        self.assertEqual(data["memory"]["scope"], "shared")

    def test_bad_effort_is_refused_by_argparse(self):
        root = self._framework_root()
        with self.assertRaises(SystemExit):
            self._main(["wren", "--root", str(root), "--role", "x",
                        "--voice", "v", "--effort", "ultra"])
        self.assertFalse((root / "cousins").exists())


class TestResumePlan(unittest.TestCase):
    """cousin-spawn --start --resume: the start-at-boot unit resumes a
    cousin's last session. Canary (2026-09-18): nothing restarted a
    cousin after a reboot, and a plain start threw the session away."""

    def _home(self, tmp, session_id="", harness=""):
        import pathlib
        root = pathlib.Path(tmp)
        (root / "config").mkdir()
        if harness:
            (root / "config" / "harness.toml").write_text(harness)
        home = root / "cousins" / "wren"
        home.mkdir(parents=True)
        run = '[runtime]\nsession_id = "%s"\n' % session_id if session_id else ""
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n' + run)
        return root, home

    RULE = ('[agent.resume]\nsession_arg = "--session-id {session_id}"\n'
            'resume_arg = "--resume {session_id}"\n')

    def test_resumes_the_saved_session(self):
        import tempfile
        from cousin_lib import spawn
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._home(tmp, "1234abcd-0000", self.RULE)
            cmd, note = spawn.resume_plan(home, root,
                                          "agent --session-id {session_id}")
            self.assertEqual(cmd, "agent --resume 1234abcd-0000")
            self.assertIn("1234abcd", note)

    def test_no_session_id_means_a_new_session(self):
        import tempfile
        from cousin_lib import spawn
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._home(tmp, "", self.RULE)
            cmd, why = spawn.resume_plan(home, root,
                                         "agent --session-id {session_id}")
            self.assertIsNone(cmd)
            self.assertIn("session_id", why)

    def test_no_resume_rule_means_a_new_session(self):
        import tempfile
        from cousin_lib import spawn
        with tempfile.TemporaryDirectory() as tmp:
            root, home = self._home(tmp, "1234abcd-0000", "")
            cmd, _ = spawn.resume_plan(home, root,
                                       "agent --session-id {session_id}")
            self.assertIsNone(cmd)

    def test_a_missing_transcript_means_a_new_session(self):
        import tempfile
        from cousin_lib import spawn
        with tempfile.TemporaryDirectory() as tmp:
            rule = 'transcripts_dir = "%s/tx"\n' % tmp + self.RULE
            root, home = self._home(tmp, "1234abcd-0000", rule)
            cmd, why = spawn.resume_plan(home, root,
                                         "agent --session-id {session_id}")
            self.assertIsNone(cmd)
            self.assertIn("transcript", why)


class PendingBootPacket(unittest.TestCase):
    """A packet a clean stop left is consumed by the next start: a
    fresh session (resume_plan declines) with the packet typed in."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n'
            '[chat]\nport = 8100\ntmux_session = "wren"\n'
            '[runtime]\nsession_id = "abc-123"\n')
        self.packet = self.home / "data" / "boot-packet-gen-0002.md"
        self.packet.write_text("BOOT PACKET FOR COUSIN: wren\n")
        self.pending = self.home / "data" / "pending-boot.json"
        self.pending.write_text(json.dumps(
            {"generation": 2, "packet": str(self.packet)}))

    def test_resume_is_declined_while_a_packet_is_pending(self):
        from cousin_lib.spawn import resume_plan
        cmd, why = resume_plan(self.home, self.home.parent.parent,
                               "agent --session-id {session_id}")
        self.assertIsNone(cmd)
        self.assertIn("closed cleanly", why)

    def test_a_record_whose_packet_is_gone_is_not_pending(self):
        from cousin_lib.spawn import pending_boot
        self.assertIsNotNone(pending_boot(self.home))
        self.packet.unlink()
        self.assertIsNone(pending_boot(self.home))

    def test_the_start_injects_the_packet_and_clears_it(self):
        from cousin_lib import spawn
        injected = []

        class FakeInjector:
            def __init__(self, session, **kw):
                self.session = session

            def inject(self, text):
                injected.append((self.session, text))

        config = spawn.CousinConfig.load(self.home)
        with mock.patch("cousin_lib.server.injection.TmuxInjector",
                        FakeInjector):
            ok = spawn._inject_pending_boot(self.home, config, tmux_bin="t",
                                            tmux_socket=None, settle=0)
        self.assertTrue(ok)
        self.assertFalse(self.pending.exists())
        self.assertEqual(injected[0][0], "wren")
        self.assertIn("closed cleanly", injected[0][1])
        self.assertIn("BOOT PACKET FOR COUSIN: wren", injected[0][1])


class TestTemplateSync(unittest.TestCase):
    """The framework part of CLAUDE.md follows the template; Identity,
    Voice and everything below the marker stay the cousin's own."""

    TEMPLATE = (
        "# {{NAME}} - {{ROLE_ONE_LINE}}\n\n## Identity\n\n{{ROLE_PARAGRAPH}}\n\n"
        "## Chat\n\nport {{PORT}} for {{SLUG}}, new wording\n\n"
        "## Voice\n\n{{VOICE_GUIDE}}\n\n## Surface\n\nrenders markdown\n\n"
        "## Append your cousin-specific sections below this line\n")

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "templates").mkdir()
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            self.TEMPLATE)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\nrole = "notes"\n'
            '[chat]\nport = 8123\n')
        (self.home / "CLAUDE.md").write_text(
            "# Wren - notes\n\n## Identity\n\nMy own role text.\n\n"
            "## Chat\n\nold wording\n\n## Voice\n\nDry and short.\n\n"
            "## Local extra\n\nkeep me\n\n"
            "## Append your cousin-specific sections below this line\n\n"
            "## Surface\n\nrenders markdown\n\n## Chat\n\nmy own chat rules\n")

    def test_sync_rebuilds_the_framework_part_only(self):
        from cousin_lib import template_sync
        out = template_sync.sync(self.home, self.root, apply=True)
        text = (self.home / "CLAUDE.md").read_text()
        self.assertTrue(out["changed"])
        self.assertIn("My own role text.", text)
        self.assertIn("Dry and short.", text)
        self.assertIn("port 8123 for wren, new wording", text)
        self.assertNotIn("old wording", text)
        self.assertIn("keep me", text)
        below = text[text.index("## Append your"):]
        self.assertNotIn("renders markdown", below)   # identical copy gone
        self.assertIn("my own chat rules", below)      # different: kept
        self.assertTrue(out["backup"].startswith(
            str(self.home / "data" / "claude-md-backups")))
        again = template_sync.sync(self.home, self.root, apply=True)
        self.assertFalse(again["changed"])

    def test_no_marker_is_refused(self):
        from cousin_lib import template_sync
        (self.home / "CLAUDE.md").write_text("# Wren\n\n## Identity\n\nx\n")
        with self.assertRaises(template_sync.SyncError):
            template_sync.sync(self.home, self.root, apply=True)


class TestRegistrySync(unittest.TestCase):
    """The MCP registry sync is additive at every level: a tool block, a
    command inside a tool the cousin already has, and a key inside a table
    it already has. Nothing the cousin wrote is changed."""

    SHIPPED = (
        '[tools.memory]\n'
        'command = "cousin-memory"\n'
        'description = "shipped wording, now mentions obsolete"\n\n'
        '[tools.memory.properties]\n'
        'topic = { type = "string", description = "the topic" }\n'
        'why = { type = "string", description = "what superseded it" }\n\n'
        '[tools.memory.commands.search]\n'
        'argv = ["search", "{query}"]\n\n'
        '[tools.memory.commands.obsolete]\n'
        'argv = ["obsolete", "{topic}"]\n'
        'options = { why = "--why" }\n\n'
        '[tools.schedule]\n'
        'command = "cousin-schedule"\n\n'
        '[tools.schedule.commands.add]\n'
        'argv = ["add", "{when}"]\n')

    MINE = (
        '[tools.memory]\n'
        'command = "cousin-memory"\n'
        'description = "my older wording"\n\n'
        '[tools.memory.properties]\n'
        'topic = { type = "string", description = "the topic" }\n\n'
        '[tools.memory.commands.search]\n'
        'argv = ["search", "{query}"]\n')

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "mcp-registry.toml.example").write_text(
            self.SHIPPED)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        self.reg = self.home / "mcp-registry.toml"
        self.reg.write_text(self.MINE)

    def _sync(self):
        from cousin_lib import template_sync
        return template_sync._registry_sync(self.home, self.root, apply=True)

    def _loaded(self):
        return tomllib.loads(self.reg.read_text())

    def test_a_command_inside_an_existing_tool_is_added(self):
        self._sync()
        cmds = self._loaded()["tools"]["memory"]["commands"]
        self.assertIn("obsolete", cmds)
        self.assertEqual(cmds["obsolete"]["argv"], ["obsolete", "{topic}"])
        self.assertEqual(cmds["obsolete"]["options"], {"why": "--why"})

    def test_a_key_inside_an_existing_table_is_added(self):
        self._sync()
        props = self._loaded()["tools"]["memory"]["properties"]
        self.assertIn("why", props)
        self.assertEqual(props["why"]["description"], "what superseded it")

    def test_a_whole_missing_tool_is_added(self):
        self._sync()
        tools = self._loaded()["tools"]
        self.assertIn("schedule", tools)
        self.assertEqual(tools["schedule"]["commands"]["add"]["argv"],
                         ["add", "{when}"])

    def test_the_cousins_own_value_is_never_overwritten(self):
        self._sync()
        memory = self._loaded()["tools"]["memory"]
        self.assertEqual(memory["description"], "my older wording")

    def test_a_second_sync_changes_nothing(self):
        self._sync()
        once = self.reg.read_text()
        out = self._sync()
        self.assertEqual(self.reg.read_text(), once)
        self.assertEqual(out["added"], [])

    def test_it_reports_what_it_added(self):
        out = self._sync()
        self.assertIn("tools.memory.commands.obsolete", out["added"])
        self.assertIn("tools.memory.properties.why", out["added"])
        self.assertIn("tools.schedule", out["added"])


class TestSyncTemplateCLI(unittest.TestCase):
    """`cousin-spawn <slug> --sync-template` is the entry an operator uses;
    it has to reach template_sync, not die in argument handling."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "templates").mkdir()
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            "# {{NAME}} - {{ROLE_ONE_LINE}}\n\n## Identity\n\n"
            "{{ROLE_PARAGRAPH}}\n\n## Voice\n\n{{VOICE_GUIDE}}\n\n"
            "## Append your cousin-specific sections below this line\n")
        home = self.root / "cousins" / "wren"
        (home / "data").mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\nrole = "notes"\n'
            '[chat]\nport = 8123\n')
        (home / "CLAUDE.md").write_text(
            "# Wren - notes\n\n## Identity\n\nMine.\n\n## Voice\n\nDry.\n\n"
            "## Append your cousin-specific sections below this line\n")

    def test_the_cli_runs_a_dry_sync(self):
        from cousin_lib import spawn
        env = {"FRAMEWORK_ROOT": str(self.root)}
        with mock.patch.dict("os.environ", env):
            rc = spawn.spawn_main(["wren", "--sync-template"])
        self.assertEqual(rc, 0)
