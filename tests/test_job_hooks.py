"""The job-tracking harness hook: subagents and background shells land
in the jobs store the console's Jobs view reads.

The harness runs the hook with a JSON payload on stdin. Pre and Post
of one tool call are correlated by tool_use_id; an agent launched in
the background is closed by its SubagentStop, by agent id. The hook
must never fail or block a tool call: every path exits 0, and an error
goes to <home>/data/job-hooks.log.

The environment the harness gives the hook is not trusted to carry
FRAMEWORK_ROOT or COUSIN_HOME: the settings write both into the hook
command, and these tests run with neither in the environment.
"""
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cousin_lib import job_hooks

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class HookCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n[chat]\nport = 8100\n')
        env = {k: v for k, v in os.environ.items()
               if k not in ("FRAMEWORK_ROOT", "COUSIN_HOME")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fire(self, payload, argv=None):
        if argv is None:
            argv = ["--home", str(self.home), "--root", str(self.root)]
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps(payload))):
            rc = job_hooks.main(argv)
        self.assertEqual(rc, 0)
        return rc

    def _jobs(self):
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                          "COUSIN_HOME": str(self.home)}):
            from cousin_lib.jobs import list_jobs
            return list_jobs()

    def _agent_pre(self, tid="toolu_a1", **extra):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                   "tool_use_id": tid, "cwd": str(self.home),
                   "tool_input": {"description": "map the tree",
                                  "prompt": "walk every module",
                                  "subagent_type": "Explore"}}
        payload["tool_input"].update(extra)
        self._fire(payload)

    def _post(self, tool, tid, response, tool_input=None):
        self._fire({"hook_event_name": "PostToolUse", "tool_name": tool,
                    "tool_use_id": tid, "tool_input": tool_input or {},
                    "tool_response": response})


class TestSubagent(HookCase):
    def test_pre_registers_a_running_subagent_job(self):
        self._agent_pre()
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["kind"], "subagent")
        self.assertEqual(job["title"], "map the tree")
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["spawned_by"], "testa")
        self.assertIn("walk every module", job["description"])
        self.assertIn("Explore", job["description"])

    def test_post_completed_marks_done_with_a_summary(self):
        self._agent_pre()
        self._post("Agent", "toolu_a1", {
            "status": "completed",
            "content": [{"type": "text", "text": "found 12 modules"}]})
        job = self._jobs()[0]
        self.assertEqual(job["status"], "done")
        self.assertIn("found 12 modules", job["result_summary"])

    def test_task_tool_name_is_tracked_too(self):
        self._fire({"hook_event_name": "PreToolUse", "tool_name": "Task",
                    "tool_use_id": "toolu_t", "tool_input": {
                        "description": "old name", "prompt": "x"}})
        self.assertEqual(self._jobs()[0]["title"], "old name")

    def test_failure_event_marks_failed(self):
        self._agent_pre()
        self._fire({"hook_event_name": "PostToolUseFailure",
                    "tool_name": "Agent", "tool_use_id": "toolu_a1",
                    "tool_input": {}, "error": "agent crashed",
                    "is_interrupt": False})
        job = self._jobs()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("agent crashed", job["result_summary"])

    def test_interrupt_marks_cancelled(self):
        self._agent_pre()
        self._fire({"hook_event_name": "PostToolUseFailure",
                    "tool_name": "Agent", "tool_use_id": "toolu_a1",
                    "tool_input": {}, "error": "stopped",
                    "is_interrupt": True})
        self.assertEqual(self._jobs()[0]["status"], "cancelled")

    def test_async_launch_stays_running_until_its_subagent_stops(self):
        self._agent_pre(run_in_background=True)
        self._post("Agent", "toolu_a1", {
            "status": "async_launched", "agentId": "ag-7",
            "description": "map the tree"})
        job = self._jobs()[0]
        self.assertEqual(job["status"], "running")
        self.assertIn("background", job["result_summary"])
        self._fire({"hook_event_name": "SubagentStop", "agent_id": "ag-7",
                    "last_assistant_message": "mapped it all"})
        job = self._jobs()[0]
        self.assertEqual(job["status"], "done")
        self.assertIn("mapped it all", job["result_summary"])

    def test_subagent_stop_for_an_untracked_agent_is_a_no_op(self):
        self._fire({"hook_event_name": "SubagentStop", "agent_id": "nope",
                    "last_assistant_message": "x"})
        self.assertEqual(self._jobs(), [])

    def test_post_without_a_pre_is_a_no_op(self):
        self._post("Agent", "toolu_never", {"status": "completed"})
        self.assertEqual(self._jobs(), [])

    def test_state_files_are_cleaned_up_after_the_close(self):
        self._agent_pre()
        self._post("Agent", "toolu_a1", {"status": "completed",
                                         "content": []})
        state = self.home / "data" / "job-hooks"
        self.assertEqual(list(state.iterdir()) if state.exists() else [], [])


class TestBash(HookCase):
    def _pre(self, background, tid="toolu_b1"):
        tool_input = {"command": "sleep 100", "description": "long wait"}
        if background:
            tool_input["run_in_background"] = True
        self._fire({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_use_id": tid, "tool_input": tool_input})

    def test_a_foreground_bash_call_is_not_tracked(self):
        self._pre(False)
        self._post("Bash", "toolu_b1", {"stdout": "ok", "stderr": "",
                                        "interrupted": False})
        self.assertEqual(self._jobs(), [])

    def test_a_background_bash_call_registers_a_shell_job(self):
        self._pre(True)
        job = self._jobs()[0]
        self.assertEqual(job["kind"], "shell")
        self.assertEqual(job["title"], "long wait")
        self.assertEqual(job["command"], "sleep 100")
        self.assertEqual(job["status"], "running")

    def test_backgrounded_result_stays_running_with_a_note(self):
        # The result only says the command was backgrounded; marking it
        # done would be a lie about a process still running.
        self._pre(True)
        self._post("Bash", "toolu_b1", {"stdout": "", "stderr": "",
                                        "interrupted": False,
                                        "backgroundTaskId": "bg-3"})
        job = self._jobs()[0]
        self.assertEqual(job["status"], "running")
        self.assertIn("bg-3", job["result_summary"])

    def test_a_finished_result_marks_done(self):
        self._pre(True)
        self._post("Bash", "toolu_b1", {"stdout": "all good\n",
                                        "stderr": "", "interrupted": False})
        job = self._jobs()[0]
        self.assertEqual(job["status"], "done")
        self.assertIn("all good", job["result_summary"])

    def test_an_interrupted_result_marks_failed(self):
        self._pre(True)
        self._post("Bash", "toolu_b1", {"stdout": "", "stderr": "",
                                        "interrupted": True})
        self.assertEqual(self._jobs()[0]["status"], "failed")

    def test_other_tools_are_ignored(self):
        self._fire({"hook_event_name": "PreToolUse", "tool_name": "Read",
                    "tool_use_id": "toolu_r", "tool_input": {}})
        self.assertEqual(self._jobs(), [])


class TestNeverFails(HookCase):
    def test_garbage_stdin_exits_zero(self):
        with mock.patch.object(sys, "stdin", io.StringIO("{nope")):
            self.assertEqual(job_hooks.main(
                ["--home", str(self.home), "--root", str(self.root)]), 0)

    def test_a_store_error_exits_zero_and_is_logged(self):
        with mock.patch("cousin_lib.jobs.register_job",
                        side_effect=RuntimeError("db locked")):
            self._agent_pre()
        log = (self.home / "data" / "job-hooks.log").read_text()
        self.assertIn("db locked", log)

    def test_no_home_anywhere_exits_zero(self):
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                 "tool_use_id": "x", "tool_input": {},
                 "cwd": str(self.root)}))):
            self.assertEqual(job_hooks.main([]), 0)

    def test_home_and_root_derive_from_the_payload_cwd(self):
        # No --home, no environment: the cwd is inside the home, the
        # root is the home's grandparent.
        sub = self.home / "notes"
        sub.mkdir()
        self._fire({"hook_event_name": "PreToolUse", "tool_name": "Agent",
                    "tool_use_id": "toolu_c", "cwd": str(sub),
                    "tool_input": {"description": "from cwd"}}, argv=[])
        self.assertEqual(self._jobs()[0]["title"], "from cwd")


class TestModuleEntry(HookCase):
    """The harness runs `python -m cousin_lib.job_hooks`: exercise that
    entry, not only main()."""

    def test_the_module_entry_registers_a_job(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                   "tool_use_id": "toolu_m",
                   "tool_input": {"description": "via -m"}}
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, "-m", "cousin_lib.job_hooks",
             "--home", str(self.home), "--root", str(self.root)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._jobs()[0]["title"], "via -m")


if __name__ == "__main__":
    unittest.main()
