"""recording: the job/activity logic the harness hook and the in-process
hooks share. Payload shapes are the harness's hook JSON."""
import json
import os
import pathlib
import tempfile
import unittest

from cousin_lib import recording
from tests._hermetic import HermeticCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    (root / "config").mkdir()
    home = root / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
    os.environ["FRAMEWORK_ROOT"] = str(root); os.environ["COUSIN_HOME"] = str(home)
    return home, root


def _pre(tool, tool_input, tid="tu-1"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": tool_input, "tool_use_id": tid, "session_id": "s", "cwd": "/"}


def _post(tool, response, tid="tu-1", tool_input=None):
    return {"hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": tool_input or {}, "tool_response": response,
            "tool_use_id": tid, "session_id": "s", "cwd": "/"}


class TestTracked(HermeticCase):
    def test_a_foreground_bash_pre_is_not_tracked_but_its_post_is(self):
        self.assertFalse(recording.is_tracked(_pre("Bash", {"command": "ls"})))
        self.assertTrue(recording.is_tracked(_post("Bash", {"stdout": ""})))
        self.assertTrue(recording.is_tracked(_pre("Agent", {"prompt": "x"})))


class TestSubagentRows(HermeticCase):
    def test_pre_registers_and_post_closes_a_subagent_job(self):
        home, root = _home(self)
        from cousin_lib import jobs
        out = recording.handle(_pre("Agent", {"prompt": "count to three",
                                              "description": "counter"}), home, root)
        self.assertIsNone(out)
        running = [j for j in jobs.list_jobs(active_only=True) if j["title"] == "counter"]
        self.assertEqual(len(running), 1)
        recording.handle(_post("Agent", {"content": [{"type": "text", "text": "1 2 3"}]}),
                         home, root)
        row = jobs.get_job(running[0]["id"])
        self.assertEqual(row["status"], "done")
        self.assertIn("1 2 3", row.get("result_summary") or "")

    def test_a_failure_closes_the_row_failed_or_cancelled(self):
        home, root = _home(self)
        from cousin_lib import jobs
        recording.handle(_pre("Agent", {"prompt": "x", "description": "doomed"}), home, root)
        recording.handle({"hook_event_name": "PostToolUseFailure", "tool_name": "Agent",
                          "tool_input": {}, "tool_use_id": "tu-1", "error": "boom",
                          "is_interrupt": True, "session_id": "s", "cwd": "/"}, home, root)
        row = [j for j in jobs.list_jobs() if j["title"] == "doomed"][0]
        self.assertEqual(row["status"], "cancelled")


class TestBackgroundShell(HermeticCase):
    def test_pre_rewrites_a_background_command_with_the_exit_trap(self):
        home, root = _home(self)
        out = recording.handle(_pre("Bash", {"command": "sleep 1", "run_in_background": True}),
                               home, root)
        updated = out["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn("sleep 1", updated)
        self.assertIn("cousin_lib.job_hooks", updated)   # the trap closes the row
        self.assertIn("--close", updated)


class TestActivity(HermeticCase):
    def test_every_post_lands_one_activity_line(self):
        home, root = _home(self)
        recording.handle(_post("Read", {"file": "x"}, tool_input={"file_path": "/tmp/x"}),
                         home, root)
        logs = list((home / "data" / "activity").glob("*"))
        self.assertTrue(logs, "no activity log written")
        text = "".join(p.read_text() for p in logs if p.is_file())
        self.assertIn("Read", text)


class TestJobHooksStillWorks(HermeticCase):
    def test_job_hooks_reexports_the_underscore_names(self):
        from cousin_lib import job_hooks
        self.assertIs(job_hooks._pre, recording.pre)
        self.assertIs(job_hooks.handle, recording.handle)


if __name__ == "__main__":
    unittest.main()
