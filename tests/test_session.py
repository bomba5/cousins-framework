"""Session bookends: the hooks a cousin runs at session start and end.

Hooks come from `cousin.toml [session]`, parsed by tomllib like every
other key in that file (the source framework hand-parsed the list with
a bracket counter; a real parser means a quoted bracket cannot break
it). Every hook sees COUSIN_HOME, COUSIN_SLUG and SESSION_PHASE; a
failing hook is reported and the rest still run; the last run lands
at data/session.json so `status` can say what happened.
"""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import session
from cousin_lib.session import load_hooks, run_phase, session_main


def _toml(*lines):
    return "\n".join(('[cousin]', 'slug = "testa"') + lines) + "\n"


class SessionCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COUSIN_SLUG", None)

    def _write_toml(self, *lines):
        (self.home / "cousin.toml").write_text(_toml(*lines))

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = session_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _state(self):
        return json.loads((self.home / "data" / "session.json").read_text())


class TestLoadHooks(SessionCase):
    def test_no_session_table_means_no_hooks(self):
        self._write_toml('[chat]', 'port = 8100')
        self.assertEqual(load_hooks(self.home, "start"), [])
        self.assertEqual(load_hooks(self.home, "end"), [])

    def test_missing_cousin_toml_means_no_hooks(self):
        self.assertEqual(load_hooks(self.home, "start"), [])

    def test_bare_strings_are_auto_named_in_order(self):
        self._write_toml('[session]',
                         'start_hooks = ["echo hello", "echo world"]')
        hooks = load_hooks(self.home, "start")
        self.assertEqual([h["cmd"] for h in hooks],
                         ["echo hello", "echo world"])
        self.assertEqual([h["name"] for h in hooks], ["step-1", "step-2"])

    def test_tables_carry_their_own_name(self):
        self._write_toml(
            '[session]',
            'end_hooks = [',
            '  {name = "sync-state", cmd = "cousin-sync-state"},',
            '  "echo done",',
            ']')
        hooks = load_hooks(self.home, "end")
        self.assertEqual(hooks[0], {"name": "sync-state",
                                    "cmd": "cousin-sync-state"})
        self.assertEqual(hooks[1], {"name": "step-2", "cmd": "echo done"})

    def test_phases_are_independent(self):
        self._write_toml('[session]',
                         'start_hooks = ["start-cmd"]',
                         'end_hooks = ["end-cmd"]')
        self.assertEqual([h["cmd"] for h in load_hooks(self.home, "start")],
                         ["start-cmd"])
        self.assertEqual([h["cmd"] for h in load_hooks(self.home, "end")],
                         ["end-cmd"])

    def test_a_real_parser_survives_brackets_inside_a_command(self):
        # The source's bracket counter would have ended the list at the
        # first "]" inside the quoted command.
        self._write_toml('[session]',
                         """start_hooks = ["grep -E '^- \\\\[ \\\\]' STATUS.md"]""")
        hooks = load_hooks(self.home, "start")
        self.assertEqual(len(hooks), 1)
        self.assertIn("STATUS.md", hooks[0]["cmd"])

    def test_a_table_without_cmd_is_refused_by_name(self):
        self._write_toml('[session]',
                         'start_hooks = [{name = "orphan"}]')
        with self.assertRaises(session.SessionConfigError) as ctx:
            load_hooks(self.home, "start")
        self.assertIn("orphan", str(ctx.exception))

    def test_unparsable_toml_is_loud(self):
        (self.home / "cousin.toml").write_text("[session\nnope")
        with self.assertRaises(session.SessionConfigError):
            load_hooks(self.home, "start")


class TestRunPhase(SessionCase):
    def test_every_hook_sees_home_slug_and_phase(self):
        self._write_toml(
            '[session]',
            'start_hooks = ["printf %s:%s:%s \\"$COUSIN_HOME\\" '
            '\\"$COUSIN_SLUG\\" \\"$SESSION_PHASE\\" '
            '> \\"$COUSIN_HOME/data/env.txt\\""]')
        result = run_phase(self.home, "start")
        self.assertEqual(result["failed"], 0)
        seen = (self.home / "data" / "env.txt").read_text()
        self.assertEqual(seen, "%s:testa:start" % self.home)

    def test_slug_from_environment_wins_over_cousin_toml(self):
        self._write_toml(
            '[session]',
            'end_hooks = ["printf %s \\"$COUSIN_SLUG\\" '
            '> \\"$COUSIN_HOME/data/slug.txt\\""]')
        with mock.patch.dict(os.environ, {"COUSIN_SLUG": "other"}):
            run_phase(self.home, "end")
        self.assertEqual((self.home / "data" / "slug.txt").read_text(),
                         "other")

    def test_a_failing_hook_is_reported_and_the_rest_still_run(self):
        self._write_toml(
            '[session]',
            'start_hooks = [',
            '  {name = "breaks", cmd = "echo boom >&2; exit 3"},',
            '  {name = "after", cmd = "touch \\"$COUSIN_HOME/data/after\\""},',
            ']')
        result = run_phase(self.home, "start")
        self.assertTrue((self.home / "data" / "after").exists())
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["ok"], 1)
        broken = [r for r in result["results"] if r["name"] == "breaks"][0]
        self.assertEqual(broken["rc"], 3)
        self.assertIn("boom", broken["output"])

    def test_skip_by_name(self):
        self._write_toml(
            '[session]',
            'start_hooks = [',
            '  {name = "noisy", cmd = "touch \\"$COUSIN_HOME/data/noisy\\""},',
            '  "touch \\"$COUSIN_HOME/data/quiet\\"",',
            ']')
        result = run_phase(self.home, "start", skip=["noisy"])
        self.assertFalse((self.home / "data" / "noisy").exists())
        self.assertTrue((self.home / "data" / "quiet").exists())
        self.assertEqual(result["skipped"], 1)

    def test_start_and_end_record_the_last_run(self):
        self._write_toml('[session]',
                         'start_hooks = ["true"]', 'end_hooks = ["true"]')
        run_phase(self.home, "start")
        state = self._state()
        self.assertTrue(state["running"])
        self.assertIsNotNone(state["last_start"])
        self.assertEqual(state["last_run"]["phase"], "start")
        run_phase(self.home, "end")
        state = self._state()
        self.assertFalse(state["running"])
        self.assertIsNotNone(state["last_end"])
        self.assertEqual(state["last_run"]["phase"], "end")
        self.assertEqual(state["last_run"]["results"][0]["rc"], 0)

    def test_no_hooks_is_a_recorded_empty_run(self):
        self._write_toml()
        result = run_phase(self.home, "start")
        self.assertEqual(result["results"], [])
        self.assertTrue(self._state()["running"])

    def test_a_corrupt_state_file_is_a_fresh_state(self):
        (self.home / "data" / "session.json").write_text("{not json")
        self._write_toml()
        run_phase(self.home, "start")
        self.assertTrue(self._state()["running"])


class TestCli(SessionCase):
    def test_start_exits_zero_when_every_hook_passes(self):
        self._write_toml('[session]', 'start_hooks = ["true"]')
        rc, out, _ = self._main(["start"])
        self.assertEqual(rc, 0)
        self.assertIn("[ok]", out)
        self.assertIn("step-1", out)

    def test_a_failed_hook_makes_the_exit_non_zero_but_not_fatal(self):
        self._write_toml('[session]',
                         'end_hooks = ["false", '
                         '{name = "still-runs", cmd = "true"}]')
        rc, out, _ = self._main(["end"])
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] step-1", out)
        self.assertIn("[ok]   still-runs", out)

    def test_repeatable_skip_flag(self):
        self._write_toml('[session]',
                         'start_hooks = [{name = "a", cmd = "false"},'
                         ' {name = "b", cmd = "false"}]')
        rc, out, _ = self._main(["start", "--skip", "a", "--skip", "b"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("[skip]"), 2)

    def test_status_prints_the_last_run_and_the_configured_hooks(self):
        self._write_toml('[session]', 'start_hooks = ["true"]',
                         'end_hooks = [{name = "sync", cmd = "true"}]')
        self._main(["start"])
        rc, out, _ = self._main(["status"])
        self.assertEqual(rc, 0)
        status = json.loads(out)
        self.assertTrue(status["running"])
        self.assertEqual(status["last_run"]["phase"], "start")
        self.assertEqual(status["end_hooks"][0]["name"], "sync")

    def test_status_before_any_run(self):
        self._write_toml()
        rc, out, _ = self._main(["status"])
        self.assertEqual(rc, 0)
        status = json.loads(out)
        self.assertIsNone(status["last_start"])
        self.assertFalse(status["running"])

    def test_no_context_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc, _, err = self._main(["status"])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err)

    def test_bad_config_is_reported_not_raised(self):
        self._write_toml('[session]', 'start_hooks = [{name = "orphan"}]')
        rc, _, err = self._main(["start"])
        self.assertEqual(rc, 2)
        self.assertIn("orphan", err)

    def test_media_specific_options_do_not_exist(self):
        # The source's --cosplay, --no-fav-drain and the voice check
        # belonged to one install's media habits; they do not ship.
        self._write_toml()
        for flag in ("--cosplay", "--no-fav-drain", "--no-voice-check"):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    session_main(["start", flag, "x"])


if __name__ == "__main__":
    unittest.main()
