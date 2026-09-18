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

    def test_tool_events_carry_both_matchers(self):
        self._apply()
        data = self._read()
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            matchers = sorted(g.get("matcher")
                              for g in data["hooks"][event])
            self.assertEqual(matchers, ["Agent|Task", "Bash"], event)

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
