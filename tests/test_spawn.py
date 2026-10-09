"""cousin-spawn: creation sequence, cleanup contract.

Tested against real temporary framework roots; nothing is mocked below
the CLI's own seams.
"""
import json
import os
import re
import pathlib
import shutil
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from cousin_lib.spawn import (
    SpawnError,
    create_cousin,
    spawn_main,
    start_cousin,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class SpawnCase(unittest.TestCase):
    def _root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name)

def _without_agent(text):
    """cousin.toml's text without its [agent] table (a new cousin gets one:
    sdk by default since 2.0.0), so a test can write its own."""
    return re.sub(r"(?ms)^\[agent\][ \t]*\n.*?(?=^\[|\Z)", "", text)


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
                    voice="Plain and helpful.")
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
        self.assertNotIn("chat", cfg)
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
            self._create(root)

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

    def test_the_new_home_is_0700_whatever_the_umask(self):
        # Closed to other users on the host from birth; an open umask
        # (0) must not leave it readable by group or other.
        root = self._framework_root()
        old = os.umask(0)
        try:
            out = self._create(root)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(out["home"].stat().st_mode), 0o700)

    def test_a_home_that_appears_before_the_mkdir_is_not_removed(self):
        # The home is made outside the cleanup: a directory another
        # process put there after the collision check is not ours, and a
        # refused create leaves it as it was.
        root = self._framework_root()
        home = root / "cousins" / "wren"
        real_mkdir = pathlib.Path.mkdir

        def racing_mkdir(path, *args, **kw):
            if path == home:
                real_mkdir(path)
                (path / "theirs.txt").write_text("x")
            return real_mkdir(path, *args, **kw)

        with mock.patch.object(pathlib.Path, "mkdir", racing_mkdir):
            with self.assertRaises(SpawnError) as ctx:
                self._create(root)
        self.assertIn("cannot create", str(ctx.exception))
        self.assertTrue((home / "theirs.txt").is_file())

def _legacy_home(root, slug="wren"):
    """A 1.x legacy home, written by hand: cousin.toml with no [agent]
    table (create_cousin makes an sdk cousin since 2.0.0)."""
    home = pathlib.Path(root) / "cousins" / slug
    for sub in ("memory", "data", "notes", "scripts"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    (home / "cousin.toml").write_text(
        '[cousin]\nslug = "%s"\nname = "%s"\nrole = "example cousin"\n'
        '\n[chat]\ntmux_session = "%s"\n' % (slug, slug.capitalize(), slug))
    return home


class TestLegacyStartRefused(CreateCase):
    """start_cousin refuses a cousin with no [agent] runner by name."""

    def test_starting_a_cousin_with_no_runner_is_refused(self):
        from cousin_lib.delivery import lane_refusal
        root = self._framework_root()
        out = {"home": _legacy_home(root)}
        with self.assertRaises(SpawnError) as ctx:
            start_cousin(out["home"], root=root)
        self.assertEqual(str(ctx.exception), lane_refusal(out["home"]))


    def test_cousin_spawn_start_refuses_with_exit_2_and_creates_nothing(self):
        import contextlib
        import io
        import os
        from cousin_lib.delivery import lane_refusal
        root = self._framework_root()
        out = {"home": _legacy_home(root)}
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COUSIN_DEFAULT_RUNNER", None)
            rc = spawn_main(["wren", "--root", str(root), "--start"])
            self.assertEqual(rc, 2)
            self.assertIn(lane_refusal(out["home"]), err.getvalue())
            # the legacy lane named as the install's default is no lane
            os.environ["COUSIN_DEFAULT_RUNNER"] = "tmux-legacy"
            rc = spawn_main(["sam", "--root", str(root), "--role", "r",
                             "--voice", "v", "--start"])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_DEFAULT_RUNNER must be one of sdk", err.getvalue())
        self.assertFalse((root / "cousins" / "sam").exists())


class TestNoChatPort(CreateCase):
    """No per-cousin chat server, so no port is allocated, written,
    rendered or accepted."""

    def test_a_new_cousin_toml_has_no_chat_table(self):
        root = self._framework_root()
        out = create_cousin(root, slug="wren", name="Wren", role="example cousin",
                            voice="Plain and helpful.", runner="fake")
        data = tomllib.loads((out["home"] / "cousin.toml").read_text())
        self.assertNotIn("chat", data)
        self.assertNotIn("port", out)
        self.assertNotIn("{{PORT}}", (out["home"] / "CLAUDE.md").read_text())
        with self.assertRaises(SpawnError) as ctx:
            create_cousin(root, slug="sam", role="r", voice="v", port=8100)
        self.assertIn("port", str(ctx.exception))
        self.assertFalse((root / "cousins" / "sam").exists())

    def test_spawn_main_rejects_port(self):
        import contextlib
        import io
        root = self._framework_root()
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as ctx:
            spawn_main(["wren", "--root", str(root), "--role", "r",
                        "--voice", "v", "--port", "8100"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((root / "cousins" / "wren").exists())


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
            "--voice", "Plain and helpful.",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("created wren at", out)
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


if __name__ == "__main__":
    unittest.main()


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


class TestPersistAgentValues(CreateCase):
    """The sdk lane's validating turn runs in a child
    process. validate_account scrubs os.environ process-wide for the turn
    (_ScrubbedAuthEnv), so run inside the console it would pull the auth
    variables from under every other thread of the console."""

    def _runner_cousin(self, extra=""):
        root = self._framework_root()
        out = self._create(root)
        path = out["home"] / "cousin.toml"
        path.write_text(_without_agent(path.read_text()) + '\n[agent]\nrunner = "sdk"\neffort = "low"\n'
                        + extra)
        return root, out["home"], path

    def test_the_sdk_models_turn_runs_out_of_process(self):
        from cousin_lib import spawn
        root, home, path = self._runner_cousin()
        with mock.patch("cousin_lib.runner.sdk.validate_account") as in_process, \
                mock.patch.object(spawn, "validate_turn_out_of_process",
                                  return_value=(0, "validate: ok")) as child:
            spawn.persist_agent_value(home, "model", "m-two", root=root)
        in_process.assert_not_called()
        child.assert_called_once_with(home, root, "m-two", "low", account="host")
        self.assertEqual(tomllib.loads(path.read_text())["agent"]["model"], "m-two")
        with mock.patch.object(spawn, "validate_turn_out_of_process",
                               return_value=(4, "validate: not_found_error")):
            with self.assertRaises(SpawnError) as ctx:
                spawn.persist_agent_value(home, "model", "m-bad", root=root)
        self.assertIn("not_found_error", str(ctx.exception))
        self.assertEqual(tomllib.loads(path.read_text())["agent"]["model"], "m-two")

    def test_an_unchanged_value_runs_no_turn_and_writes_nothing(self):
        from cousin_lib import spawn
        root, home, path = self._runner_cousin('model = "m-one"\n')
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with mock.patch.object(spawn, "validate_turn_out_of_process") as child, \
                mock.patch.object(spawn, "framework_event") as event:
            spawn.persist_agent_value(home, "model", "m-one", root=root)
            spawn.persist_agent_value(home, "effort", "low", root=root)
        child.assert_not_called()
        event.assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


class TestPersistAgentValuesSeveral(CreateCase):
    """persist_agent_values is the ONE [agent] write path, the
    console's settings panel and the model and effort routes both: the
    lane's checks (agent_settings.validate), the sdk model's validating
    turn in a child, then agent_settings.apply."""

    def _runner_cousin(self, extra=""):
        root = self._framework_root()
        out = self._create(root)
        path = out["home"] / "cousin.toml"
        path.write_text(_without_agent(path.read_text()) + '\n[agent]\nrunner = "sdk"\neffort = "low"\n'
                        + extra)
        return root, out["home"], path

    def test_several_keys_one_write_one_turn(self):
        from cousin_lib import spawn
        root, home, path = self._runner_cousin()
        with mock.patch.object(spawn, "validate_turn_out_of_process",
                               return_value=(0, "validate: ok")) as child:
            changed = spawn.persist_agent_values(
                home, {"model": "m-two", "effort": "high", "auto_start": False,
                       "rollover_at_percent": 70, "sessions": {"meeting": "own"}},
                root=root)
        # the turn runs on the effort being written with it
        child.assert_called_once_with(home, root, "m-two", "high", account="host")
        self.assertEqual(sorted(changed), ["auto_start", "effort", "model",
                                           "rollover_at_percent", "sessions"])
        agent = tomllib.loads(path.read_text())["agent"]
        self.assertEqual((agent["model"], agent["effort"], agent["auto_start"]),
                         ("m-two", "high", False))
        self.assertEqual(agent["rollover_at_percent"], 70.0)
        self.assertEqual(agent["sessions"], {"meeting": "own"})

    def test_no_turn_without_a_model_change(self):
        from cousin_lib import spawn
        root, home, path = self._runner_cousin('model = "m-one"\n')
        with mock.patch.object(spawn, "validate_turn_out_of_process") as child:
            changed = spawn.persist_agent_values(
                home, {"model": "m-one", "auto_start": False}, root=root)
        child.assert_not_called()
        self.assertEqual(changed, ["auto_start"])

    def test_a_refusal_names_each_key_and_writes_nothing(self):
        from cousin_lib import agent_settings, spawn
        root, home, path = self._runner_cousin()
        before = path.read_bytes()
        with mock.patch.object(spawn, "validate_turn_out_of_process") as child:
            with self.assertRaises(agent_settings.SettingsError) as ctx:
                spawn.persist_agent_values(
                    home, {"effort": "ultra", "rollover_at_percent": 400,
                           "sessions": {"operator": "own"}}, root=root)
        child.assert_not_called()
        self.assertEqual(set(ctx.exception.errors),
                         {"effort", "rollover_at_percent", "sessions"})
        self.assertEqual(path.read_bytes(), before)

    def test_a_failed_turn_is_a_model_error_and_writes_nothing(self):
        from cousin_lib import agent_settings, spawn
        root, home, path = self._runner_cousin()
        before = path.read_bytes()
        with mock.patch.object(spawn, "validate_turn_out_of_process",
                               return_value=(4, "validate: not_found_error")):
            with self.assertRaises(agent_settings.SettingsError) as ctx:
                spawn.persist_agent_values(home, {"model": "m-bad", "auto_start": False},
                                           root=root)
        self.assertEqual(list(ctx.exception.errors), ["model"])
        self.assertIn("not_found_error", ctx.exception.errors["model"])
        self.assertEqual(path.read_bytes(), before)

    def test_removing_a_key_and_a_same_session_mode_are_changes_only_when_real(self):
        from cousin_lib import spawn
        root, home, path = self._runner_cousin('auto_start = false\n')
        with mock.patch.object(spawn, "framework_event") as event:
            self.assertEqual(spawn.persist_agent_values(
                home, {"rollover_at_percent": None, "sessions": {"peer": "primary"}},
                root=root), [])
            event.assert_not_called()
            self.assertEqual(spawn.persist_agent_values(
                home, {"auto_start": None}, root=root), ["auto_start"])
        self.assertNotIn("auto_start", tomllib.loads(path.read_text())["agent"])

    def test_the_turn_runs_on_the_account_and_effort_being_written(self):
        """Not the account in cousin.toml, which still names the old one
        while the change is pending."""
        from cousin_lib import spawn
        root, home, path = self._runner_cousin()
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "accounts.toml").write_text('[accounts.alt]\nkind = "claude-login"\n')
        seen = []

        def turn(h, r, model, effort, **kw):
            seen.append((model, effort, kw.get("account")))
            return 0, "ok"
        with mock.patch.object(spawn, "validate_turn_out_of_process", side_effect=turn):
            spawn.persist_agent_values(home, {"model": "m-two", "account": "alt",
                                              "effort": "max"}, root=root)
        self.assertEqual(seen, [("m-two", "max", "alt")])
        with mock.patch.object(spawn, "validate_turn_out_of_process",
                               return_value=(4, "validate: nope")):
            with self.assertRaises(Exception) as ctx:
                spawn.persist_agent_values(home, {"model": "m-3", "account": None}, root=root)
        self.assertIn("account host", str(ctx.exception))

    def test_an_inline_sessions_table_is_a_refusal_not_a_crash(self):
        from cousin_lib import agent_settings, spawn
        root, home, path = self._runner_cousin('sessions = { person = "own" }\n')
        before = path.read_bytes()
        with self.assertRaises(agent_settings.SettingsError) as ctx:
            spawn.persist_agent_values(home, {"sessions": {"meeting": "own"}}, root=root)
        self.assertIn("sessions", ctx.exception.errors)
        self.assertEqual(path.read_bytes(), before)

    def test_an_int_for_a_float_in_the_file_is_no_change(self):
        from cousin_lib import spawn
        root, home, path = self._runner_cousin('rollover_at_percent = 80.0\n')
        with mock.patch.object(spawn, "framework_event") as event:
            self.assertEqual(spawn.persist_agent_values(
                home, {"rollover_at_percent": 80}, root=root), [])
        event.assert_not_called()

    def test_a_malformed_sessions_value_is_the_parsers_refusal(self):
        from cousin_lib import agent_settings, spawn
        root, home, path = self._runner_cousin()
        with self.assertRaises(agent_settings.SettingsError) as ctx:
            spawn.persist_agent_values(home, {"sessions": "own"}, root=root)
        self.assertIn("sessions", ctx.exception.errors)

    def test_a_tmux_legacy_cousin_is_refused(self):
        from cousin_lib import spawn
        root = self._framework_root()
        home = _legacy_home(root)
        with self.assertRaises(SpawnError):
            spawn.persist_agent_values(home, {"effort": "high"}, root=root)


class TestPersistAgentValuesOnTmux(CreateCase):
    """The tmux kind's pane takes --model and --effort, so both
    are its [agent] keys; no validating turn runs (no pane here)."""

    def test_model_and_effort_are_written_with_no_turn(self):
        from cousin_lib import spawn
        root = self._framework_root()
        home = self._create(root)["home"]
        path = home / "cousin.toml"
        path.write_text(_without_agent(path.read_text()) + '\n[agent]\nrunner = "tmux"\n')
        with mock.patch.object(spawn, "validate_turn_out_of_process") as child:
            self.assertTrue(spawn.persist_agent_value(home, "effort", "high", root=root))
            self.assertTrue(spawn.persist_agent_value(home, "model", "m-two", root=root))
        child.assert_not_called()
        agent = tomllib.loads(path.read_text())["agent"]
        self.assertEqual((agent["effort"], agent["model"]), ("high", "m-two"))


class TestValidateTurnOutOfProcess(unittest.TestCase):
    """The child's verdict is one JSON line on stdout; the parent reads
    only that, under a timeout, and its own os.environ is never edited."""

    def _run(self, script, **kw):
        import sys
        from cousin_lib import spawn
        return spawn.validate_turn_out_of_process(
            Path("/nonexistent/home"), Path("/nonexistent/root"), "m-two", "low",
            command=[sys.executable, "-c", script], **kw)

    def test_the_childs_json_verdict_is_the_answer(self):
        rc, line = self._run(
            "import json, sys; print('noise'); "
            "print(json.dumps({'rc': 4, 'line': ' '.join(sys.argv[1:])}))")
        self.assertEqual(rc, 4)
        self.assertEqual(line, "--home /nonexistent/home --root /nonexistent/root"
                               " --model m-two --effort low --timeout 90")

    def test_the_account_being_written_is_named_to_the_child(self):
        rc, line = self._run(
            "import json, sys; print(json.dumps({'rc': 4, 'line': ' '.join(sys.argv[1:])}))",
            account="alt")
        self.assertIn(" --account alt ", line + " ")

    def test_the_child_resolves_a_named_account_not_the_files(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from cousin_lib.runner import validate_turn
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "accounts.toml").write_text('[accounts.alt]\nkind = "claude-login"\n')
            home = root / "cousins" / "wren"
            home.mkdir(parents=True)
            (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n\n[agent]\nrunner = "sdk"\n')
            seen = []

            def fake(account, root_, **kw):
                seen.append(account.name)
                return 0, "ok"
            with mock.patch("cousin_lib.runner.sdk.validate_account", fake), \
                    redirect_stdout(io.StringIO()):
                validate_turn.main(["--home", str(home), "--root", str(root),
                                    "--model", "m", "--account", "alt"])
                validate_turn.main(["--home", str(home), "--root", str(root), "--model", "m"])
                rc = validate_turn.main(["--home", str(home), "--root", str(root),
                                         "--model", "m", "--account", "nope"])
        self.assertEqual(seen, ["alt", "host"])
        self.assertEqual(rc, 2)

    def test_the_child_gets_no_auth_variable_and_the_parents_env_is_untouched(self):
        from cousin_lib import accounts
        env = {"ANTHROPIC_API_KEY": "parent-key", "CLAUDE_CONFIG_DIR": "/parent"}
        with mock.patch.dict("os.environ", env):
            import os
            before = dict(os.environ)
            rc, line = self._run(
                "import json, os; from cousin_lib import accounts; "
                "print(json.dumps({'rc': 0, 'line': ','.join("
                "v for v in accounts.AUTH_VARS if v in os.environ)}))")
            self.assertEqual(dict(os.environ), before)
        self.assertEqual((rc, line), (0, ""), accounts.AUTH_VARS)

    def test_a_child_without_a_verdict_fails_with_its_words(self):
        rc, line = self._run("import sys; sys.stderr.write('boom\\n'); sys.exit(3)")
        self.assertEqual(rc, 4)
        self.assertIn("boom", line)

    def test_a_bad_byte_is_replaced_never_raised(self):
        """A byte that is not UTF-8 must not raise out
        of the decode (a 500, and the child left unwaited)."""
        rc, line = self._run(
            "import sys; sys.stdout.buffer.write(b'\\xff\\xfe junk\\n'); "
            "sys.stdout.buffer.write(b'{\"rc\": 4, \"line\": \"validate: \\xc3\\x28\"}\\n')")
        self.assertEqual(rc, 4)
        self.assertIn("validate:", line)
        rc, line = self._run("import sys; sys.stderr.buffer.write(b'bad \\xff byte\\n'); "
                             "sys.exit(1)")
        self.assertEqual(rc, 4)
        self.assertIn("bad", line)

    def test_a_child_past_the_budget_is_killed_and_fails(self):
        import time
        started = time.monotonic()
        rc, line = self._run("import time; time.sleep(30)", timeout=0.2, grace=0.3)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(rc, 4)
        self.assertIn("no answer within", line)

    def test_the_real_entry_answers_in_json(self):
        """The module the console runs, as a real child: a home with no
        cousin.toml is a configuration error before any model call."""
        from cousin_lib import spawn
        rc, line = spawn.validate_turn_out_of_process(
            Path("/nonexistent/home"), Path("/nonexistent/root"), "m-two", None)
        self.assertEqual(rc, 2, line)
        self.assertIn("cousin.toml", line)


class TestCreateWithRuntimeOptions(CreateCase):
    def test_create_writes_runtime_heartbeat_and_scope(self):
        root = self._framework_root()
        out = self._create(root, model="m-one", effort="medium",
                           heartbeat=600, memory_scope="both")
        data = tomllib.loads((out["home"] / "cousin.toml").read_text())
        # no runner named is an sdk cousin, whose model and effort
        # are the [agent] keys its runner reads; no [runtime] table
        self.assertEqual(data["agent"], {"runner": "sdk", "model": "m-one",
                                         "effort": "medium"})
        self.assertNotIn("runtime", data)
        self.assertEqual(data["heartbeat"]["context_beat_seconds"], 600)
        self.assertEqual(data["memory"]["scope"], "shared")
        from cousin_lib.config import CousinConfig
        cfg = CousinConfig.load(out["home"])
        self.assertEqual((cfg.heartbeat_seconds, cfg.memory_scope),
                         (600, "shared"))

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
            "--model", "m-one", "--effort", "max",
            "--heartbeat", "900", "--memory-scope", "shared",
        ])
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(
            (root / "cousins" / "wren" / "cousin.toml").read_text())
        self.assertEqual(data["agent"], {"runner": "sdk", "model": "m-one",
                                         "effort": "max"})
        self.assertNotIn("runtime", data)
        self.assertEqual(data["heartbeat"]["context_beat_seconds"], 900)
        self.assertEqual(data["memory"]["scope"], "shared")

    def test_bad_effort_is_refused_by_argparse(self):
        root = self._framework_root()
        with self.assertRaises(SystemExit):
            self._main(["wren", "--root", str(root), "--role", "x",
                        "--voice", "v", "--effort", "ultra"])
        self.assertFalse((root / "cousins").exists())


class TestResumeFlagGone(CreateCase):
    """--resume resumed a legacy tmux session from [agent.resume]; a
    runner resumes its own session, so the flag is gone, not ignored."""

    def test_resume_is_refused_by_argparse(self):
        import contextlib
        import io
        root = self._framework_root()
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as caught:
            spawn_main(["wren", "--root", str(root), "--role", "x", "--voice", "v",
                        "--resume"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("unrecognized arguments: --resume", err.getvalue())
        self.assertFalse((root / "cousins").exists())


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

    def test_a_retired_section_stays_until_pruned(self):
        # meeting 11 D: the lane's mechanics left the template for the
        # contract; an existing home keeps its copy until the operator has
        # seen the diff and asks for the prune
        from cousin_lib import template_sync
        claude = self.home / "CLAUDE.md"
        claude.write_text(claude.read_text().replace(
            "## Local extra\n\nkeep me\n\n",
            "## Local extra\n\nkeep me\n\n## Session bookends\n\nrun cousin-session\n\n"))
        _old, new, notes = template_sync.plan(self.home, self.root)
        self.assertIn("run cousin-session", new)
        self.assertTrue(any("Session bookends: retired" in n and "--prune-retired" in n
                            for n in notes), notes)
        _old, new, notes = template_sync.plan(self.home, self.root, prune=True)
        self.assertNotIn("run cousin-session", new)
        self.assertIn("keep me", new)
        self.assertTrue(any("Session bookends: retired from the template; removed" in n
                            for n in notes), notes)

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


class TestRegistrySyncCarriesJobRun(unittest.TestCase):
    """A cousin whose registry predates `job run` gains the command and its
    two properties on sync, and the result still validates."""

    def setUp(self):
        import tempfile
        from cousin_lib import template_sync
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        shipped = (pathlib.Path(__file__).resolve().parents[1] / "config"
                   / "mcp-registry.toml.example").read_text()
        (self.root / "config" / "mcp-registry.toml.example").write_text(shipped)
        old = []
        for path, body in template_sync._blocks(shipped):
            if path == "tools.job.commands.run":
                continue
            if path == "tools.job.properties":
                body = [l for _k, ls in template_sync._entries(body)
                        if _k not in ("argv", "log") for l in ls]
            old.append(("" if path is None else "[%s]\n" % path) + "".join(body))
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        self.reg = self.home / "mcp-registry.toml"
        self.reg.write_text("".join(old))
        before = tomllib.loads(self.reg.read_text())["tools"]["job"]
        assert "run" not in before["commands"] and "argv" not in before["properties"]

    def test_run_and_its_properties_are_added_and_validate(self):
        from cousin_lib import mcp_server, template_sync
        out = template_sync._registry_sync(self.home, self.root, apply=True)
        self.assertIn("tools.job.commands.run", out["added"])
        self.assertIn("tools.job.properties.argv", out["added"])
        self.assertIn("tools.job.properties.log", out["added"])
        job = mcp_server.load_registry(self.reg)["tools"]["job"]
        self.assertIn("run", job["commands"])
        self.assertEqual(job["commands"]["run"]["argv"][-1], "{argv}")
        self.assertEqual(job["properties"]["argv"]["type"], "array")


def ts_inline(table):
    """A TOML inline table for a flat dict of strings, booleans and
    string lists."""
    import json as _json
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, list):
            return "[" + ", ".join(_json.dumps(x) for x in v) + "]"
        return _json.dumps(v)
    return "{ " + ", ".join("%s = %s" % (k, val(v)) for k, v in table.items()) + " }"


class TestRegistrySyncCorrectsShippedText(unittest.TestCase):
    """A cousin's registry that still carries the job tool's pre-`run`
    description and `kind` text (what the sync only ever ADDED to, never
    corrected) gets the current wording; a cousin's own rewrite of the
    same field is never touched; correcting is idempotent.
    """

    def setUp(self):
        import tempfile
        from cousin_lib import template_sync
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.shipped = (pathlib.Path(__file__).resolve().parents[1]
                        / "config" / "mcp-registry.toml.example").read_text()
        (self.root / "config" / "mcp-registry.toml.example").write_text(
            self.shipped)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        self.reg = self.home / "mcp-registry.toml"
        self.template_sync = template_sync

    def _old_registry(self):
        """The shipped registry with every description _MIGRATIONS records
        rolled back to the earlier wording it names, the way a cousin
        synced before this fix would still have it."""
        ts = self.template_sync
        olds = {(table, spec.split(".")[0]): pairs[0][0]
                for (table, spec), pairs in ts._MIGRATIONS.items()}
        out = []
        for path, body in ts._blocks(self.shipped):
            new_entries = []
            for key, lines in ts._entries(body):
                old = olds.get((path, key)) if path and key else None
                if old:
                    text = ts._field("description").sub(
                        lambda m, v=old: m.group(1) + json.dumps(v),
                        "".join(lines), count=1)
                    lines = [text]
                new_entries.append((key, lines))
            out.append(("" if path is None else "[%s]\n" % path)
                       + "".join(l for _, ls in new_entries for l in ls))
        return "".join(out)

    def _sync(self):
        return self.template_sync._registry_sync(self.home, self.root,
                                                  apply=True)

    def test_stale_options_and_argv_and_a_hash_in_a_string_are_migrated(self):
        """`options` and `argv` a past release shipped are the
        framework's own too: a remember that still maps no `scope` gets
        the shipped table, whole. A `#` inside a string (a description
        naming `raw:<file>#<line>`) is not a comment: the keys after it
        stay visible to the sync, so their old text migrates as well."""
        old = {("tools.memory.commands.remember", "options"):
               'options = { level = "--level", cite = "--cite", derived_from = "--derived-from" }\n',
               ("tools.memory.properties", "keyword"):
               'keyword = { type = "string", optional = true, description = "filter (recall)" }\n'}
        ts = self.template_sync
        out = []
        for path, body in ts._blocks(self.shipped):
            lines = []
            for key, entry in ts._entries(body):
                lines.extend([old[(path, key)]] if (path, key) in old else entry)
            out.append(("" if path is None else "[%s]\n" % path) + "".join(lines))
        self.reg.write_text("".join(out))
        result = self._sync()
        self.assertIn("tools.memory.commands.remember.options", result["corrected"])
        self.assertIn("tools.memory.properties.keyword", result["corrected"])
        now = tomllib.loads(self.reg.read_text())["tools"]["memory"]
        shipped = tomllib.loads(self.shipped)["tools"]["memory"]
        self.assertEqual(now["commands"]["remember"]["options"],
                         shipped["commands"]["remember"]["options"])
        self.assertEqual(now["properties"]["keyword"], shipped["properties"]["keyword"])
        self.assertEqual(self._sync()["corrected"], [])
        self.assertEqual(ts._depth('a = { d = "raw:<f>#<n>" }\n'), 0)

    def test_a_property_table_a_release_changed_is_migrated_whole(self):
        """An enum is not a string field: a `kind` that still offers the
        retired `shell` gets the shipped table whole. A cousin's own edit
        of the table (here an extra field) blocks the whole migration."""
        ts = self.template_sync
        old_kind = next(v for v in ts.SHIPPED_BEFORE[("tools.job.properties", "kind")]
                        if "shell" in v.get("enum", []))
        line = "kind = " + ts_inline(old_kind) + "\n"
        out = []
        for path, body in ts._blocks(self.shipped):
            lines = []
            for key, entry in ts._entries(body):
                lines.extend([line] if (path, key) == ("tools.job.properties", "kind") else entry)
            out.append(("" if path is None else "[%s]\n" % path) + "".join(lines))
        self.reg.write_text("".join(out))
        self.assertIn("tools.job.properties.kind", self._sync()["corrected"])
        kind = tomllib.loads(self.reg.read_text())["tools"]["job"]["properties"]["kind"]
        self.assertNotIn("shell", kind["enum"])
        self.assertEqual(kind, tomllib.loads(self.shipped)["tools"]["job"]["properties"]["kind"])
        edited = dict(old_kind, mine=True)
        self.reg.write_text("".join(out).replace(line, "kind = " + ts_inline(edited) + "\n"))
        self._sync()
        self.assertIn("shell", tomllib.loads(self.reg.read_text())["tools"]["job"]["properties"]["kind"]["enum"])

    def test_old_shipped_text_is_corrected(self):
        self.reg.write_text(self._old_registry())
        before = tomllib.loads(self.reg.read_text())["tools"]["job"]
        self.assertEqual(before["description"],
                         self.template_sync._MIGRATIONS[
                             ("tools.job", "description")][0][0])
        out = self._sync()
        for field in ("tools.job.description", "tools.job.properties.kind",
                     "tools.job.properties.title",
                     "tools.job.properties.desc"):
            self.assertIn(field, out["corrected"])
        job = tomllib.loads(self.reg.read_text())["tools"]["job"]
        shipped_job = tomllib.loads(self.shipped)["tools"]["job"]
        self.assertEqual(job["description"], shipped_job["description"])
        self.assertEqual(job["properties"]["kind"]["description"],
                         shipped_job["properties"]["kind"]["description"])
        self.assertEqual(job["properties"]["title"]["description"],
                         shipped_job["properties"]["title"]["description"])
        self.assertEqual(job["properties"]["desc"]["description"],
                         shipped_job["properties"]["desc"]["description"])
        # kept valid TOML, and everything else about kind untouched
        self.assertEqual(job["properties"]["kind"]["enum"],
                         ["subagent", "build", "other"])

    def test_the_cousins_own_wording_is_kept(self):
        text = self._old_registry()
        old_line = "description = %s" % json.dumps(
            self.template_sync._MIGRATIONS[
                ("tools.job", "description")][0][0])
        self.assertIn(old_line, text)
        text = text.replace(
            old_line, 'description = "my own wording for the job tool"', 1)
        self.reg.write_text(text)
        out = self._sync()
        self.assertNotIn("tools.job.description", out["corrected"])
        job = tomllib.loads(self.reg.read_text())["tools"]["job"]
        self.assertEqual(job["description"],
                         "my own wording for the job tool")

    def test_a_second_sync_is_a_no_op(self):
        self.reg.write_text(self._old_registry())
        self._sync()
        once = self.reg.read_text()
        out = self._sync()
        self.assertEqual(self.reg.read_text(), once)
        self.assertEqual(out["corrected"], [])
        self.assertEqual(out["added"], [])


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


class TestRegistrySyncOrder(unittest.TestCase):
    """An added table joins the tool it belongs to, so the file stays
    readable for whoever edits it next."""

    SHIPPED = (
        '[tools.memory]\ncommand = "cousin-memory"\n\n'
        '[tools.memory.commands.search]\nargv = ["search"]\n\n'
        '[tools.memory.commands.obsolete]\nargv = ["obsolete"]\n\n'
        '[tools.meeting]\ncommand = "cousin-meeting"\n\n'
        '[tools.meeting.commands.say]\nargv = ["say"]\n')

    MINE = (
        '[tools.memory]\ncommand = "cousin-memory"\n\n'
        '[tools.memory.commands.search]\nargv = ["search"]\n\n'
        '[tools.meeting]\ncommand = "cousin-meeting"\n')

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

    def test_an_added_table_follows_its_own_tool(self):
        from cousin_lib import template_sync
        template_sync._registry_sync(self.home, self.root, apply=True)
        heads = [l for l in self.reg.read_text().splitlines()
                 if l.startswith("[")]
        self.assertEqual(heads, [
            "[tools.memory]",
            "[tools.memory.commands.search]",
            "[tools.memory.commands.obsolete]",
            "[tools.meeting]",
            "[tools.meeting.commands.say]"])
        tools = tomllib.loads(self.reg.read_text())["tools"]
        self.assertIn("obsolete", tools["memory"]["commands"])
        self.assertIn("say", tools["meeting"]["commands"])
