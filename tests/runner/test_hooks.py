"""Hooks in-process: each callback driven with the SDK's input shape,
asserting the store effect. No SDK needed: `callbacks` is plain."""
import asyncio
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import activity
from cousin_lib.runner import hooks
from cousin_lib.runner.envelope import CONTEXT_MARK
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name); (root / "config").mkdir()
    home = root / "cousins" / "wren"
    for sub in ("data", "memory", "notes"):
        (home / sub).mkdir(parents=True)
    # No embedding seam here: recall qualifies keyword hits only when opted in.
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n'
                                      '[memory]\nrecall_keyword_only = true\n')
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
        self.assertTrue(ctx.startswith("[fw-recall] "))
        self.assertIn("Router (memory:reference_router.md)", ctx)
        recalls = [e["payload"] for e in self.stream.tail() if e["kind"] == "recall"]
        self.assertEqual(recalls, [{"hits": 1}])

    def test_no_hits_means_no_context_and_no_error(self):
        out = _run(self.cbs["UserPromptSubmit"](self._base(
            "UserPromptSubmit", prompt="zzzq qqqz xxyzzy wobbling frobnicator"), None, {}))
        self.assertNotIn("additionalContext", out.get("hookSpecificOutput", {}))
        recalls = [e["payload"] for e in self.stream.tail() if e["kind"] == "recall"]
        self.assertEqual(recalls, [{"hits": 0}])
        self.assertFalse(any(e["kind"] == "hook" for e in self.stream.tail()))


    def test_the_chat_servers_gates_apply(self):
        (self.home / "memory" / "reference_router.md").write_text("# Router\nreboots on Sundays\n")
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        out = _run(self.cbs["UserPromptSubmit"](self._base(
            "UserPromptSubmit", prompt="when does the router reboot on Sundays?"), None, {}))
        self.assertEqual(out, {})   # keyword-only without the opt-in: silent
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n'
                                               '[memory]\nrecall_keyword_only = true\n')
        out = _run(self.cbs["UserPromptSubmit"](self._base(
            "UserPromptSubmit", prompt="router Sundays"), None, {}))
        self.assertEqual(out, {})   # under min_chars
        self.assertFalse(any(e["kind"] == "hook" for e in self.stream.tail()))

    def test_recall_searches_the_body_not_the_envelope(self):
        seen = []
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root, machine=self.machine,
                              stream=self.stream,
                              recall=lambda body: (seen.append(body), (None, 0))[1],
                              body_for_prompt=lambda prompt: "the newest row body")
        _run(cbs["UserPromptSubmit"](self._base(
            "UserPromptSubmit", prompt="[operator:priya] 2026-09-23 Priya: hi"), None, {}))
        self.assertEqual(seen, ["the newest row body"])

    def test_a_prompt_that_carries_context_is_not_recalled_twice(self):
        seen = []
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root, machine=self.machine,
                              stream=self.stream,
                              recall=lambda body: (seen.append(body), ("[fw-recall] x", 1))[1])
        out = _run(cbs["UserPromptSubmit"](self._base(
            "UserPromptSubmit", prompt="hi\n\n%s\n[fw-recall] y" % CONTEXT_MARK), None, {}))
        self.assertEqual(out, {}); self.assertEqual(seen, [])
        recalls = [e["payload"] for e in self.stream.tail() if e["kind"] == "recall"]
        self.assertEqual(recalls, [{"hits": 0, "skipped": "context present"}])

    def test_an_empty_body_is_not_searched(self):
        seen = []
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root, machine=self.machine,
                              stream=self.stream,
                              recall=lambda body: (seen.append(body), ("[fw-recall] x", 1))[1],
                              body_for_prompt=lambda prompt: "")
        out = _run(cbs["UserPromptSubmit"](self._base(
            "UserPromptSubmit", prompt="[peer:toki] a peer row"), None, {}))
        self.assertEqual(out, {}); self.assertEqual(seen, [])
        recalls = [e["payload"] for e in self.stream.tail() if e["kind"] == "recall"]
        self.assertEqual(recalls, [{"hits": 0, "skipped": "empty body"}])

    def test_a_slow_recall_is_cut_off_at_the_budget_off_the_loop(self):
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root, machine=self.machine,
                              stream=self.stream,
                              recall=lambda body: (time.sleep(1.0), ("[fw-recall] late", 1))[1])

        async def timed():
            t = time.monotonic()
            out = await cbs["UserPromptSubmit"](self._base(
                "UserPromptSubmit", prompt="anything at all here"), None, {})
            return out, time.monotonic() - t
        with mock.patch.object(hooks, "RECALL_BUDGET_S", 0.1):
            out, took = _run(timed())
        self.assertEqual(out, {}); self.assertLess(took, 0.5)
        recalls = [e["payload"] for e in self.stream.tail() if e["kind"] == "recall"]
        self.assertEqual(recalls, [{"hits": 0, "timed_out": True}])


class TestRecorderChecksThePolicy(HooksCase):
    """Matched PreToolUse callbacks run concurrently in the CLI: the
    recorder cannot rely on the policy callback having run first."""

    def _cbs(self, toml):
        from cousin_lib.runner import policy
        (self.home / "policy.toml").write_text(toml)
        return hooks.callbacks(self.home, slug="wren", root=self.root, machine=self.machine,
                               stream=self.stream, policy=policy.Policy.load(self.home))

    def test_a_denied_agent_pre_tool_use_leaves_no_job_row(self):
        from cousin_lib import jobs
        cbs = self._cbs('deny_tools = ["Agent", "Task"]\n')
        out = _run(cbs["PreToolUse"](self._base(
            "PreToolUse", tool_name="Agent", tool_input={"prompt": "x", "description": "denied"},
            tool_use_id="tu-d"), "tu-d", {}))
        self.assertEqual(out, {})
        self.assertEqual([j for j in jobs.list_jobs() if j["title"] == "denied"], [])

    def test_a_denied_background_bash_gets_no_row_and_no_rewrite(self):
        from cousin_lib import jobs
        cbs = self._cbs("deny_bash_patterns = ['sleep']\n")
        out = _run(cbs["PreToolUse"](self._base(
            "PreToolUse", tool_name="Bash",
            tool_input={"command": "sleep 2", "run_in_background": True},
            tool_use_id="tu-b"), "tu-b", {}))
        self.assertEqual(out, {})
        self.assertEqual([j for j in jobs.list_jobs() if j["kind"] == "shell"], [])

    def test_an_asked_agent_is_not_recorded_either(self):
        from cousin_lib import jobs
        cbs = self._cbs('ask = ["Agent"]\n')
        _run(cbs["PreToolUse"](self._base(
            "PreToolUse", tool_name="Agent", tool_input={"prompt": "x", "description": "asked"},
            tool_use_id="tu-a"), "tu-a", {}))
        self.assertEqual([j for j in jobs.list_jobs() if j["title"] == "asked"], [])

    def test_an_allowed_agent_is_still_recorded(self):
        from cousin_lib import jobs
        cbs = self._cbs('deny_tools = ["WebFetch"]\n')
        _run(cbs["PreToolUse"](self._base(
            "PreToolUse", tool_name="Agent", tool_input={"prompt": "x", "description": "allowed"},
            tool_use_id="tu-ok"), "tu-ok", {}))
        self.assertEqual(len([j for j in jobs.list_jobs() if j["title"] == "allowed"]), 1)


class TestOffTheLoop(HooksCase):
    def test_the_recorder_and_the_checkpoints_run_on_a_worker_thread(self):
        import threading
        seen = {}

        class Checkpoints:
            @staticmethod
            def write_session_checkpoint(home, *, slug):
                seen["stop"] = threading.get_ident()
                return home / "data" / "session-checkpoint.md"

            @staticmethod
            def write_pre_compact_checkpoint(home, *, slug):
                seen["precompact"] = threading.get_ident()
                return home / "data" / "pre-compact-checkpoint.md"

        cbs = hooks.callbacks(self.home, slug="wren", root=self.root, machine=self.machine,
                              stream=self.stream, checkpoints=Checkpoints,
                              recorder=lambda payload: seen.__setitem__(
                                  "record", threading.get_ident()))

        async def drive():
            seen["loop"] = threading.get_ident()
            await cbs["PostToolUse"](self._base("PostToolUse", tool_name="Read", tool_input={},
                                                tool_use_id="t", tool_response={}), "t", {})
            await cbs["Stop"](self._base("Stop", stop_hook_active=False), None, {})
            await cbs["PreCompact"](self._base("PreCompact", trigger="auto"), None, {})
        _run(drive())
        for key in ("record", "stop", "precompact"):
            self.assertIn(key, seen)
            self.assertNotEqual(seen[key], seen["loop"], key)


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

    def test_only_a_permission_prompt_notification_moves_the_state(self):
        self.machine.to("running")
        _run(self.cbs["Notification"](self._base(
            "Notification", message="waiting for input", notification_type="idle_prompt"), None, {}))
        self.assertEqual(self.machine.state, "running")
        _run(self.cbs["Notification"](self._base(
            "Notification", message="Bash needs approval",
            notification_type="permission_prompt"), None, {}))
        self.assertEqual(self.machine.state, "waiting_permission")
        self.assertEqual(sum(e["kind"] == "permission" for e in self.stream.tail()), 2)

    def test_the_permission_move_runs_under_the_runners_lock(self):
        class Lock:
            held, acquired = False, 0

            def __enter__(self):
                self.held = True; self.acquired += 1

            def __exit__(self, *exc):
                self.held = False
        lock, moves = Lock(), []
        machine = StateMachine(on_change=lambda o, n, d: moves.append((n, lock.held)))
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root, machine=machine,
                              stream=self.stream, lock=lock)
        machine.to("running")
        moves.clear()
        _run(cbs["PermissionRequest"](self._base(
            "PermissionRequest", tool_name="Bash", tool_input={}), None, {}))
        self.assertEqual(lock.acquired, 1)
        self.assertEqual(moves, [("waiting_permission", True)])
        self.assertFalse(lock.held)

    def test_notification_when_idle_only_records(self):
        _run(self.cbs["Notification"](self._base(
            "Notification", message="needs input", notification_type="permission_prompt"), None, {}))
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
