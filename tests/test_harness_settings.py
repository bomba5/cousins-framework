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


class TestHooksDir(unittest.TestCase):
    def test_hooks_dir_is_the_checkout_hooks_whatever_the_cwd(self):
        self.assertEqual(harness_settings.hooks_dir(), _REPO_ROOT / "hooks")


if __name__ == "__main__":
    unittest.main()


class TestTmuxKindSettings(SettingsCase):
    """Phase 11 Task 3, the settings half (I7, P11-8, P11-12): the tmux kind's
    pane runs the real CLI, so its project settings carry the kind's switches,
    the policy's deny rules and the four bridge hooks; an sdk or legacy home
    gets none of them, and a switch back to sdk removes exactly those."""

    KEYS = {"editorMode": "normal", "autoContinueAtUsageLimit": False,
            "autoCompactEnabled": False, "attribution": {"commit": "", "pr": ""},
            "remoteControlAtStartup": False}
    EVENTS = ("UserPromptSubmit", "Stop", "Notification", "SessionStart")

    def setUp(self):
        super().setUp()
        (self.home / "policy.toml").write_text(
            'deny_tools = ["WebFetch", "mcp__cousin__send"]\n')

    def _bridge(self, data, event):
        return [c for c in self._commands(data, event) if "cousin_lib.runner.tmux_hook" in c]

    def test_the_kinds_settings_are_written(self):
        self._apply(kind="tmux", python="/usr/bin/python3")
        data = self._read()
        for key, value in self.KEYS.items():
            self.assertEqual(data[key], value, key)
        self.assertIn("cousin", data["enabledMcpjsonServers"])
        self.assertEqual(data["permissions"]["deny"], ["WebFetch", "mcp__cousin__send"])
        for event in self.EVENTS:
            self.assertEqual(self._bridge(data, event), [shlex.join(
                ["/usr/bin/python3", "-m", "cousin_lib.runner.tmux_hook", "--home",
                 str(self.home.resolve()), event])], event)
        self.assertTrue(self._commands(data, "PreToolUse"))       # the job hooks stay (parity)

    def test_an_sdk_or_legacy_home_gets_none_of_them(self):
        self._apply()
        data = self._read()
        for key in self.KEYS:
            self.assertNotIn(key, data)
        self.assertNotIn("permissions", data)
        for event in self.EVENTS:
            self.assertEqual(self._bridge(data, event), [])

    def test_a_second_run_changes_nothing_and_keeps_the_operators_own(self):
        path = settings_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"],
                                                    "allow": ["Read"]}, "theme": "dark"}))
        self._apply(kind="tmux")
        first = path.read_text()
        self._apply(kind="tmux")
        self.assertEqual(path.read_text(), first)
        data = self._read()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["permissions"]["allow"], ["Read"])
        self.assertEqual(data["permissions"]["deny"],
                         ["Bash(rm:*)", "WebFetch", "mcp__cousin__send"])

    def test_the_switch_back_removes_exactly_the_kinds_settings(self):
        path = settings_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}, "theme": "dark"}))
        self._apply()
        before = self._read()
        self._apply(kind="tmux")
        harness_settings.remove_kind_settings(self.home)
        self.assertEqual(self._read(), before)
        harness_settings.remove_kind_settings(self.home)          # idempotent
        self.assertEqual(self._read(), before)
