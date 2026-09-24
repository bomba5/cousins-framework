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
    """Round 3 review: the two writes are ordered by DIRECTION, not
    always the same way, so neither crash window ever needs a
    claim-by-match to heal (round 2's claim-by-match reopened Critical
    1: an operator's own pre-existing includeCoAuthoredBy: false got
    adopted on a turn-off and then deleted on a later turn-on).
    Turning off ADDS keys: the marker goes first (it can only ever
    under-claim, never over-claim, across a crash). Turning on REMOVES
    keys: settings.json goes first, and a stale marker claim is
    reconciled away (_reconcile_owned) at the start of the next run.
    Minor: the marker's write is skipped when its content did not
    change, same as settings.json's own short-circuit."""

    def _cousin_agent(self, extra):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n\n[agent]\n' + extra)

    def _marker_path(self):
        return harness_settings._marker_path(self.home)

    def test_turn_off_crash_after_the_marker_before_settings_json_heals(self):
        # the marker claims the keys it is about to add BEFORE
        # settings.json is touched; a crash right there must still let
        # the next run add exactly what was already claimed.
        self._cousin_agent("commit_attribution = false\n")
        with mock.patch.object(harness_settings, "_write_settings_file",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._apply()
        self.assertEqual(json.loads(self._marker_path().read_text()),
                         {"includeCoAuthoredBy": False,
                          "attribution": {"commit": "", "pr": ""}})
        self.assertFalse(settings_path(self.home).exists())   # never reached
        self._apply()   # the real run: heals by adding what was claimed
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], False)
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})

    def test_turn_on_crash_after_settings_json_before_the_marker_heals(self):
        # settings.json is written first on turn-on; a crash before the
        # marker catches up leaves it claiming keys that are already
        # gone. The next run must reconcile that away, not choke on it
        # or resurrect the keys.
        self._cousin_agent("commit_attribution = false\n")
        self._apply()   # a normal off cycle: both keys claimed
        stale = json.loads(self._marker_path().read_text())
        self.assertIn("includeCoAuthoredBy", stale)
        # simulate the crash: settings.json already has the keys
        # removed (as turn-on's own write would leave them), the marker
        # untouched - written directly, not through apply_project_settings,
        # to model the process dying right after its settings.json write.
        path = settings_path(self.home)
        data = json.loads(path.read_text())
        del data["includeCoAuthoredBy"]
        del data["attribution"]
        path.write_text(json.dumps(data, indent=2) + "\n")
        self._cousin_agent("commit_attribution = true\n")
        self._apply()
        self.assertFalse(self._marker_path().exists())   # reconciled away
        data = self._read()
        self.assertNotIn("includeCoAuthoredBy", data)
        self.assertNotIn("attribution", data)

    def test_operators_preexisting_identical_value_survives_off_then_on(self):
        # Critical 1, reopened by round 2's claim-by-match: the operator
        # already had includeCoAuthoredBy: false, with no marker entry,
        # before this module ever ran. Off must never claim it; on must
        # then have nothing of its own to remove.
        path = settings_path(self.home)
        path.parent.mkdir()
        path.write_text(json.dumps({
            "includeCoAuthoredBy": False,
            "attribution": {"commit": "", "pr": ""},
        }, indent=2) + "\n")
        self._cousin_agent("commit_attribution = false\n")
        self._apply()
        self.assertFalse(self._marker_path().exists())   # never claimed
        data = self._read()
        self.assertIs(data["includeCoAuthoredBy"], False)   # untouched
        self._cousin_agent("commit_attribution = true\n")
        self._apply()
        data = self._read()   # still there: on had nothing owned to remove
        self.assertIs(data["includeCoAuthoredBy"], False)
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})

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


class TestTmuxKindSettings(SettingsCase):
    """Phase 11 Task 3, the settings half (I7, P11-8, P11-12): the tmux kind's
    pane runs the real CLI, so its project settings carry the kind's switches,
    the policy's deny rules and the four bridge hooks; an sdk or legacy home
    gets none of them, and a switch back to sdk removes exactly those."""

    KEYS = {"editorMode": "normal", "autoContinueAtUsageLimit": False,
            "autoCompactEnabled": False, "remoteControlAtStartup": False}
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
        for event in self.EVENTS:                                 # the CLI bounds the bridge hook too
            self.assertEqual([h.get("timeout") for g in data["hooks"][event] for h in g["hooks"]
                              if "cousin_lib.runner.tmux_hook" in h["command"]], [5], event)

    def test_deny_tools_only_policy_warns_nothing(self):
        out = self._apply(kind="tmux")
        self.assertEqual(out["warnings"], [])

    def test_ask_and_deny_bash_patterns_are_warned_not_silently_dropped(self):
        """I5: neither reaches the pane under --dangerously-skip-permissions
        (only deny_tools does); the gap is declared to whoever writes the
        settings, not just to docs/reference/runners.md."""
        (self.home / "policy.toml").write_text(
            'deny_tools = ["WebFetch"]\n'
            'deny_bash_patterns = ["rm -rf"]\n'
            'ask = ["Bash"]\n')
        out = self._apply(kind="tmux")
        self.assertEqual(len(out["warnings"]), 2)
        bash_warning = next(w for w in out["warnings"] if "deny_bash_patterns" in w)
        ask_warning = next(w for w in out["warnings"] if "ask" in w)
        self.assertIn("do not reach the tmux pane", bash_warning)
        self.assertIn("do not reach the tmux pane", ask_warning)
        # deny_tools itself still reaches permissions.deny, unaffected
        data = self._read()
        self.assertEqual(data["permissions"]["deny"], ["WebFetch"])

    def test_an_sdk_home_is_never_warned(self):
        (self.home / "policy.toml").write_text(
            'deny_bash_patterns = ["rm -rf"]\nask = ["Bash"]\n')
        out = self._apply()
        self.assertEqual(out["warnings"], [])

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

    def _agent(self, extra):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n\n[agent]\nrunner = "tmux"\n' + extra)

    def test_attribution_on_the_pane_follows_commit_attribution(self):
        """#112 owns attribution on every kind: the tmux kind forces
        nothing, so the operator's commit_attribution decides the pane's."""
        self._agent("")
        self._apply(kind="tmux")
        data = self._read()
        self.assertNotIn("attribution", data)                 # unset: the CLI's stock behaviour
        self.assertNotIn("includeCoAuthoredBy", data)
        self._agent("commit_attribution = false\n")
        self._apply(kind="tmux")
        data = self._read()
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})
        self.assertIs(data["includeCoAuthoredBy"], False)
        harness_settings.remove_kind_settings(self.home)      # a switch back keeps #112's own keys
        data = self._read()
        self.assertEqual(data["attribution"], {"commit": "", "pr": ""})
        self.assertIs(data["includeCoAuthoredBy"], False)
        self._agent("commit_attribution = true\n")
        self._apply(kind="tmux")
        data = self._read()
        self.assertNotIn("attribution", data)
        self.assertNotIn("includeCoAuthoredBy", data)

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
