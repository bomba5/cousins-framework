"""Per-cousin harness project settings.

Each cousin carries its own <home>/.claude/settings.json so the hooks
that run in its sessions are ITS hooks, named by absolute path with the
home written into the command, and so the `cousin` MCP server is
approved without a hand edit. The writer merges: keys it does not own
are kept, and a second run changes nothing.
"""
import json
import pathlib
import shlex
import tempfile
import unittest
from unittest import mock

from cousin_lib import harness_settings
from cousin_lib.harness_settings import (
    SettingsError,
    apply_project_settings,
    settings_path,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class SettingsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n')

    def _apply(self, **kw):
        return apply_project_settings(self.home, root=self.root, **kw)

    def _read(self):
        return json.loads(settings_path(self.home).read_text())

    def _commands(self, data, event):
        return [h["command"] for group in data["hooks"].get(event, [])
                for h in group["hooks"]]


class TestFreshWrite(SettingsCase):
    def test_writes_under_the_home_dot_claude(self):
        self._apply()
        self.assertEqual(settings_path(self.home),
                         self.home / ".claude" / "settings.json")
        self.assertTrue(settings_path(self.home).is_file())

    def test_the_cousin_mcp_server_is_approved(self):
        self._apply()
        self.assertEqual(self._read()["enabledMcpjsonServers"], ["cousin"])

    def test_shell_hooks_are_absolute_and_carry_the_home(self):
        self._apply()
        data = self._read()
        for event, script in (("SessionStart", "session_init.sh"),
                              ("PreCompact", "pre_compact.sh"),
                              ("Stop", "session_checkpoint.sh")):
            cmds = self._commands(data, event)
            self.assertEqual(len(cmds), 1, event)
            argv = shlex.split(cmds[0])
            self.assertEqual(argv[0], str(_REPO_ROOT / "hooks" / script))
            self.assertTrue(pathlib.Path(argv[0]).is_absolute())
            self.assertTrue(pathlib.Path(argv[0]).is_file())
            self.assertEqual(argv[1:], [str(self.home)])

    def test_a_home_with_spaces_is_quoted_in_the_command(self):
        self.home = self.root / "cousins" / "with space"
        self.home.mkdir(parents=True)
        self._apply()
        cmd = self._commands(self._read(), "Stop")[0]
        self.assertEqual(shlex.split(cmd)[1], str(self.home))

    def test_no_hooks_dir_skips_the_shell_hooks_and_says_so(self):
        out = self._apply(hooks_root=self.root / "nowhere")
        data = self._read()
        self.assertEqual(self._commands(data, "Stop"), [])
        self.assertIn("session_checkpoint.sh", " ".join(out["missing"]))


class TestJobHooks(SettingsCase):
    """The job-tracking hook is wired for subagent and Bash calls, their
    failures, and subagent stops, under the interpreter that wrote the
    settings, with home and root in the command."""

    def test_tool_events_carry_their_matchers(self):
        # Pre registers jobs, so only subagents and shells; Post and its
        # failure take every tool (no matcher), because each call also
        # lands in the activity log.
        self._apply()
        data = self._read()
        pre = sorted(g.get("matcher") for g in data["hooks"]["PreToolUse"])
        self.assertEqual(pre, ["Agent|Task", "Bash"])
        for event in ("PostToolUse", "PostToolUseFailure"):
            groups = data["hooks"][event]
            self.assertEqual(len(groups), 1, event)
            self.assertNotIn("matcher", groups[0], event)

    def test_subagent_stop_is_wired(self):
        self._apply()
        self.assertEqual(len(self._commands(self._read(), "SubagentStop")),
                         1)

    def test_command_runs_the_module_with_home_and_root(self):
        self._apply(python="/opt/venv/bin/python3")
        cmd = self._commands(self._read(), "PreToolUse")[0]
        self.assertEqual(shlex.split(cmd), [
            "/opt/venv/bin/python3", "-m", "cousin_lib.job_hooks",
            "--home", str(self.home), "--root", str(self.root)])

    def test_default_interpreter_is_the_running_one(self):
        import sys
        self._apply()
        cmd = self._commands(self._read(), "PreToolUse")[0]
        self.assertEqual(shlex.split(cmd)[0], sys.executable)

    def test_a_new_interpreter_replaces_the_old_entry(self):
        self._apply(python="/old/python3")
        self._apply(python="/new/python3")
        cmds = self._commands(self._read(), "PreToolUse")
        self.assertEqual(len(cmds), 2)  # one per matcher, not four
        self.assertTrue(all(c.startswith("/new/python3") for c in cmds))


class TestMerge(SettingsCase):
    def test_foreign_keys_and_foreign_hooks_are_kept(self):
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({
            "model": "kept",
            "enabledMcpjsonServers": ["other"],
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "/opt/mine.sh"}]}]},
        }))
        self._apply()
        data = self._read()
        self.assertEqual(data["model"], "kept")
        self.assertEqual(data["enabledMcpjsonServers"], ["other", "cousin"])
        cmds = self._commands(data, "Stop")
        self.assertIn("/opt/mine.sh", cmds)
        self.assertEqual(len(cmds), 2)

    def test_a_second_run_changes_nothing(self):
        self._apply()
        first = settings_path(self.home).read_text()
        self._apply()
        self.assertEqual(settings_path(self.home).read_text(), first)

    def test_an_entry_from_a_moved_checkout_is_replaced_not_doubled(self):
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command",
             "command": "/old/checkout/hooks/session_checkpoint.sh /x"}]}]}}))
        self._apply()
        cmds = self._commands(self._read(), "Stop")
        self.assertEqual(len(cmds), 1)
        self.assertNotIn("/old/checkout", cmds[0])

    def test_unreadable_settings_are_refused_not_clobbered(self):
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text("{not json")
        with self.assertRaises(SettingsError):
            self._apply()
        self.assertEqual(path.read_text(), "{not json")

    def test_a_non_list_server_list_is_refused(self):
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({"enabledMcpjsonServers": "cousin"}))
        with self.assertRaises(SettingsError):
            self._apply()


class TestAttribution(SettingsCase):
    """Tracker #112: config.commit_attribution decides whether the
    settings file carries includeCoAuthoredBy: false and an empty
    attribution object - the tmux lane's own reach for the same
    outcome the SDK runner gets through --settings."""

    def _cousin_agent(self, extra):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n\n[agent]\n' + extra)

    def test_default_true_adds_nothing(self):
        self._apply()
        data = self._read()
        self.assertNotIn("includeCoAuthoredBy", data)
        self.assertNotIn("attribution", data)

    def test_cousin_override_false_adds_the_keys(self):
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], False)
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})

    def test_install_default_false_adds_the_keys(self):
        (self.root / "config").mkdir()
        (self.root / "config" / "harness.toml").write_text(
            "[agent]\ncommit_attribution = false\n")
        self._apply()
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], False)
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})

    def test_cousin_override_true_wins_over_install_false(self):
        (self.root / "config").mkdir()
        (self.root / "config" / "harness.toml").write_text(
            "[agent]\ncommit_attribution = false\n")
        self._cousin_agent("commit_attribution = true\n")
        self._apply()
        data = self._read()
        self.assertNotIn("includeCoAuthoredBy", data)
        self.assertNotIn("attribution", data)

    def test_a_second_run_off_changes_nothing(self):
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        first = settings_path(self.home).read_text()
        self._apply()
        self.assertEqual(settings_path(self.home).read_text(), first)

    def test_turning_it_back_on_removes_what_the_framework_added(self):
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        self._cousin_agent("commit_attribution = true\n")
        self._apply()
        data = self._read()
        self.assertNotIn("includeCoAuthoredBy", data)
        self.assertNotIn("attribution", data)

    def test_operators_own_keys_are_never_clobbered_turning_off(self):
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({
            "includeCoAuthoredBy": True,
            "attribution": {"commit": "operator wrote this", "pr": "x"},
        }))
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], True)
        self.assertEqual(data["attribution"]["commit"], "operator wrote this")

    def test_operators_own_keys_are_never_clobbered_turning_on(self):
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({"includeCoAuthoredBy": True}))
        self._apply()   # commit_attribution unset anywhere: True, nothing to remove
        self.assertIs(self._read()["includeCoAuthoredBy"], True)

    def test_an_operator_value_matching_the_frameworks_own_shape_survives(self):
        # Critical 1 (review round 1): value equality alone is not
        # ownership. An operator who happens to write exactly what the
        # framework would write, with commit_attribution unset (true,
        # nothing this module ever wrote), must not have it deleted.
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({
            "includeCoAuthoredBy": False,
            "attribution": {"commit": "", "pr": ""},
        }))
        self._apply()   # commit_attribution unset anywhere: true
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], False)
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})

    def test_the_framework_writes_it_then_turning_on_removes_it(self):
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], False)   # sanity: it did write
        self._cousin_agent("commit_attribution = true\n")
        self._apply()
        data = self._read()
        self.assertNotIn("includeCoAuthoredBy", data)
        self.assertNotIn("attribution", data)

    def test_the_operator_editing_the_framework_written_value_survives(self):
        self._cousin_agent("commit_attribution = false\n")
        self._apply()   # the framework writes and records it
        # the operator hand-edits the file afterwards
        path = settings_path(self.home)
        data = json.loads(path.read_text())
        data["includeCoAuthoredBy"] = True
        path.write_text(json.dumps(data))
        self._cousin_agent("commit_attribution = true\n")
        self._apply()   # turning on: the recorded value no longer matches
        self.assertIs(self._read()["includeCoAuthoredBy"], True)

    def test_the_marker_never_lands_in_settings_json(self):
        # the marker is a sidecar under data/, never a key in the file
        # the harness itself reads (see the module docstring for why)
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        data = self._read()
        self.assertTrue(all(not str(k).startswith("_cousin") for k in data))


class TestAttributionCrashSafety(SettingsCase):
    """Round 2 review. Important: settings.json must land before the
    marker, so a crash in the gap heals on the next run instead of
    stranding a key forever. Minor: the marker's write is skipped when
    its content did not change, same as settings.json's own
    short-circuit."""

    def _cousin_agent(self, extra):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n\n[agent]\n' + extra)

    def _marker_path(self):
        return harness_settings._marker_path(self.home)

    def test_settings_json_lands_before_the_marker_even_on_a_failed_write(self):
        # the order itself: a marker write that raises must never have
        # stopped settings.json from reaching disk first.
        self._cousin_agent("commit_attribution = false\n")
        with mock.patch.object(harness_settings, "_write_owned_attribution",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._apply()
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], False)
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})
        self.assertFalse(self._marker_path().exists())   # never reached

    def test_a_crash_between_the_two_writes_heals_on_the_next_turn_on(self):
        # simulates the crash directly: settings.json already holds the
        # off values (as apply_project_settings would have left them),
        # but the marker was never written (the process died first).
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({
            "includeCoAuthoredBy": False,
            "attribution": {"commit": "", "pr": ""},
        }, indent=2) + "\n")
        self.assertFalse(self._marker_path().exists())
        # commit_attribution is still false: one more run must adopt
        # (not strand) the values already on disk.
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        self.assertTrue(self._marker_path().exists())
        # proof the heal actually recorded ownership, not just a no-op:
        # turning on now removes what it adopted.
        self._cousin_agent("commit_attribution = true\n")
        self._apply()
        data = self._read()
        self.assertNotIn("includeCoAuthoredBy", data)
        self.assertNotIn("attribution", data)

    def test_a_crash_before_any_write_leaves_both_files_as_they_were(self):
        # the reverse order: nothing this module touches happens before
        # settings.json's own read+merge step, so a failure while
        # building `data` (here: an unreadable settings.json) leaves the
        # marker untouched too - there is nothing to roll back.
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text("{not json")
        marker = self._marker_path()
        marker.write_text(json.dumps({"includeCoAuthoredBy": False}))
        before = marker.read_text()
        self._cousin_agent("commit_attribution = true\n")
        with self.assertRaises(SettingsError):
            self._apply()
        self.assertEqual(path.read_text(), "{not json")
        self.assertEqual(marker.read_text(), before)

    def test_the_marker_write_is_skipped_when_unchanged(self):
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        before_mtime = self._marker_path().stat().st_mtime_ns
        before_bytes = self._marker_path().read_bytes()
        with mock.patch("tempfile.mkstemp") as mkstemp:
            self._apply()
        mkstemp.assert_not_called()
        self.assertEqual(self._marker_path().stat().st_mtime_ns, before_mtime)
        self.assertEqual(self._marker_path().read_bytes(), before_bytes)

    def test_the_marker_write_still_happens_when_it_changed(self):
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        self._cousin_agent("commit_attribution = true\n")
        self._apply()
        self.assertFalse(self._marker_path().exists())


class TestHooksDir(unittest.TestCase):
    def test_hooks_dir_is_the_checkout_hooks_whatever_the_cwd(self):
        self.assertEqual(harness_settings.hooks_dir(), _REPO_ROOT / "hooks")


if __name__ == "__main__":
    unittest.main()
