"""Hooks in-process: each callback driven with the SDK's input shape,
asserting the store effect. No SDK needed: `callbacks` is plain."""
import asyncio
import os
import pathlib
import tempfile
import unittest

from cousin_lib import activity
from cousin_lib.runner import hooks
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name); (root / "config").mkdir()
    home = root / "cousins" / "wren"
    for sub in ("data", "memory", "notes"):
        (home / sub).mkdir(parents=True)
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
    os.environ["FRAMEWORK_ROOT"] = str(root); os.environ["COUSIN_HOME"] = str(home)
    return root, home


def _run(coro):
    return asyncio.run(coro)


class HooksCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.root, self.home = _home(self)
        self.stream = EventStream(self.home, "hooks-test")
        self.machine = StateMachine(on_change=lambda o, n, d: self.stream.append(
            "state", {"from": o, "to": n, "detail": d}))
        self.cbs = hooks.callbacks(self.home, slug="wren", root=self.root,
                                   machine=self.machine, stream=self.stream)

    def _base(self, event, **kw):
        return {"hook_event_name": event, "session_id": "s", "transcript_path": "/dev/null",
                "cwd": str(self.home), **kw}


class TestRecording(HooksCase):
    def test_pre_and_post_record_a_subagent_job_in_process(self):
        from cousin_lib import jobs
        out = _run(self.cbs["PreToolUse"](self._base(
            "PreToolUse", tool_name="Agent", tool_input={"prompt": "x", "description": "hooked"},
            tool_use_id="tu-1"), "tu-1", {}))
        self.assertEqual(out, {})
        _run(self.cbs["PostToolUse"](self._base(
            "PostToolUse", tool_name="Agent", tool_input={}, tool_use_id="tu-1",
            tool_response={"content": [{"type": "text", "text": "done"}]}), "tu-1", {}))
        row = [j for j in jobs.list_jobs() if j["title"] == "hooked"][0]
        self.assertEqual(row["status"], "done")

    def test_a_background_bash_pre_returns_the_updated_input(self):
        out = _run(self.cbs["PreToolUse"](self._base(
            "PreToolUse", tool_name="Bash", tool_input={"command": "sleep 2", "run_in_background": True},
            tool_use_id="tu-2"), "tu-2", {}))
        self.assertIn("--close", out["hookSpecificOutput"]["updatedInput"]["command"])

    def test_every_post_lands_one_activity_line(self):
        _run(self.cbs["PostToolUse"](self._base(
            "PostToolUse", tool_name="Read", tool_input={"file_path": "/tmp/x"}, tool_use_id="tu-3",
            tool_response={"file": {"content": ""}}), "tu-3", {}))
        # The activity log is activity.activity_path(home): data/activity/<day>.log.
        lines = activity.activity_path(self.home).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("Read", lines[0])


class TestRecall(HooksCase):
    def test_prompt_submit_adds_recall_context_when_memory_has_hits(self):
        (self.home / "memory" / "reference_router.md").write_text(
            "# Router\nThe router lives at 10.0.0.1 and reboots on Sundays.\n")
        out = _run(self.cbs["UserPromptSubmit"](self._base(
            "UserPromptSubmit", prompt="when does the router reboot?"), None, {}))
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn("[fw-recall]", ctx); self.assertIn("reference_router", ctx)
        self.assertTrue(any(e["kind"] == "recall" for e in self.stream.tail()))

    def test_no_hits_means_no_context_and_no_error(self):
        out = _run(self.cbs["UserPromptSubmit"](self._base("UserPromptSubmit", prompt="zzzq"), None, {}))
        self.assertNotIn("additionalContext", out.get("hookSpecificOutput", {}))
        recalls = [e["payload"] for e in self.stream.tail() if e["kind"] == "recall"]
        self.assertEqual(recalls, [{"hits": 0}])
        self.assertFalse(any(e["kind"] == "hook" for e in self.stream.tail()))


class TestCheckpointsAndState(HooksCase):
    def test_stop_and_precompact_write_the_checkpoint_files(self):
        (self.home / "data" / "last-activity.txt").write_text("2026-09-23T10:00: testing hooks\n")
        _run(self.cbs["Stop"](self._base("Stop", stop_hook_active=False), None, {}))
        text = (self.home / "data" / "session-checkpoint.md").read_text()
        self.assertIn("testing hooks", text); self.assertIn("## What was happening", text)
        out = _run(self.cbs["PreCompact"](self._base("PreCompact", trigger="auto",
                                                     custom_instructions=None), None, {}))
        self.assertIn("checkpoint", out.get("systemMessage", "").lower())
        self.assertTrue((self.home / "data" / "pre-compact-checkpoint.md").exists())
        self.assertEqual(sum(e["kind"] == "checkpoint" for e in self.stream.tail()), 2)

    def test_permission_request_moves_a_running_machine_to_waiting(self):
        self.machine.to("running")
        _run(self.cbs["PermissionRequest"](self._base(
            "PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf /"}), None, {}))
        self.assertEqual(self.machine.state, "waiting_permission")
        events = [e for e in self.stream.tail() if e["kind"] == "permission"]
        self.assertEqual(events[-1]["payload"]["tool_name"], "Bash")

    def test_a_second_request_while_waiting_only_records(self):
        self.machine.to("running")
        for tool in ("Bash", "Write"):
            _run(self.cbs["PermissionRequest"](self._base(
                "PermissionRequest", tool_name=tool, tool_input={}), None, {}))
        self.assertEqual(self.machine.state, "waiting_permission")
        moves = [e["payload"]["to"] for e in self.stream.tail() if e["kind"] == "state"]
        self.assertEqual(moves, ["running", "waiting_permission"])
        self.assertEqual(sum(e["kind"] == "permission" for e in self.stream.tail()), 2)
        self.assertFalse(any(e["kind"] == "hook" for e in self.stream.tail()))

    def test_notification_when_idle_only_records(self):
        _run(self.cbs["Notification"](self._base(
            "Notification", message="needs input", notification_type="idle"), None, {}))
        self.assertEqual(self.machine.state, "idle")
        self.assertTrue(any(e["kind"] == "permission" for e in self.stream.tail()))


class TestNeverRaises(HooksCase):
    def test_a_failing_callback_records_and_returns_empty(self):
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root, machine=self.machine,
                              stream=self.stream, recorder=lambda payload: 1 / 0)
        out = _run(cbs["PostToolUse"](self._base("PostToolUse", tool_name="X", tool_input={},
                                                 tool_use_id="t", tool_response={}), "t", {}))
        self.assertEqual(out, {})
        errs = [e for e in self.stream.tail() if e["kind"] == "hook" and "ZeroDivision" in e["payload"]["error"]]
        self.assertEqual(len(errs), 1)


class TestBuildHooks(HooksCase):
    def test_build_hooks_wraps_every_event_in_matchers(self):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            self.skipTest("claude-agent-sdk not installed")
        table = hooks.build_hooks(self.home, slug="wren", root=self.root, machine=self.machine,
                                  stream=self.stream)
        for ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStop",
                   "UserPromptSubmit", "Stop", "PreCompact", "Notification", "PermissionRequest"):
            self.assertIn(ev, table); self.assertTrue(table[ev][0].hooks)
        self.assertEqual(table["PreToolUse"][0].matcher, "Agent|Task|Bash")
        self.assertIsNone(table["PostToolUse"][0].matcher)


if __name__ == "__main__":
    unittest.main()
