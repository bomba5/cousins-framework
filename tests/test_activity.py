"""The activity log and the job logs the harness hooks write: every
tool call a cousin or its subagents make lands as one readable line,
and every subagent and background shell job carries a readable log,
with nothing asked of the cousin (operator, 2026-09-18: "transparent
for the cousin ... they just do")."""
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cousin_lib import activity, job_hooks
from tests.test_job_hooks import HookCase, _REPO_ROOT


class Lines(unittest.TestCase):
    def test_describe_names_the_useful_part(self):
        self.assertEqual(activity.describe_call("Read", {"file_path": "/a"}),
                         "/a")
        self.assertIn("git status", activity.describe_call(
            "Bash", {"command": "git status", "description": "show"}))
        self.assertTrue(activity.describe_call(
            "Bash", {"command": "sleep 9", "run_in_background": True})
            .startswith("[bg] "))
        self.assertEqual(activity.describe_call(
            "Grep", {"pattern": "foo", "path": "src"}), "foo in src")
        self.assertIn('"to": "wren"', activity.describe_call(
            "mcp__cousin__send", {"to": "wren", "text": "hi"}))

    def test_a_line_says_ok_fail_and_who(self):
        ok = activity.activity_line({
            "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": "/x.py"}, "tool_response": {}})
        self.assertRegex(ok, r"^\d\d:\d\d:\d\d  Edit\s+ok\s+/x\.py$")
        fail = activity.activity_line({
            "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "make"}, "error": "exit 2: no rule",
            "agent_type": "Explore"})
        self.assertIn("FAIL", fail)
        self.assertIn("[Explore] make  -> exit 2: no rule", fail)
        long = activity.activity_line({
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "x" * 2000}})
        self.assertLess(len(long), 300)


class ActivityFromHooks(HookCase):
    def _log_text(self):
        folder = self.home / "data" / "activity"
        return "".join(p.read_text() for p in folder.glob("*.log"))

    def test_every_tool_call_is_one_line_and_no_job(self):
        self._post("Read", "toolu_r1", {}, {"file_path": "/etc/hosts"})
        self._post("Bash", "toolu_b1", {"stdout": "x", "interrupted": False},
                   {"command": "ls -la"})
        self._fire({"hook_event_name": "PostToolUseFailure",
                    "tool_name": "Edit", "tool_use_id": "toolu_e1",
                    "tool_input": {"file_path": "/a.py"},
                    "error": "old_string not found"})
        text = self._log_text()
        self.assertIn("Read", text)
        self.assertIn("/etc/hosts", text)
        self.assertIn("ls -la", text)
        self.assertIn("old_string not found", text)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertEqual(self._jobs(), [])


class ShellJobLog(HookCase):
    def _wrapped(self, command):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_use_id": "toolu_s1",
                   "tool_input": {"command": command, "description": "logged",
                                  "run_in_background": True}}
        out = io.StringIO()
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", out):
            job_hooks.main(["--home", str(self.home), "--root",
                            str(self.root)])
        return json.loads(out.getvalue())[
            "hookSpecificOutput"]["updatedInput"]["command"]

    def test_output_goes_to_the_job_log_and_the_harness(self):
        shells = [s for s in ("/run/current-system/sw/bin/bash", "/bin/bash",
                              "/run/current-system/sw/bin/zsh", "/bin/zsh")
                  if os.path.exists(s)]
        for shell in shells:
            with self.subTest(shell=shell):
                self.setUp()
                wrapped = self._wrapped("echo out-line; echo err-line >&2;"
                                        " exit 4")
                env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
                proc = subprocess.run([shell, "-c", wrapped],
                                      capture_output=True, text=True,
                                      env=env, timeout=60)
                self.assertEqual(proc.returncode, 4)
                self.assertIn("out-line", proc.stdout)
                job = self._jobs()[0]
                self.assertEqual(job["exit_code"], 4)
                text = pathlib.Path(job["log_path"]).read_text()
                self.assertIn("# background shell: logged", text)
                self.assertIn("out-line", text)
                self.assertIn("err-line", text)


class SubagentJobLog(HookCase):
    def _transcript(self, session, tid):
        folder = self.root / "proj" / session / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-abc.meta.json").write_text(
            json.dumps({"toolUseId": tid, "agentType": "Explore"}))
        lines = [
            {"type": "user", "timestamp": "2026-09-18T19:00:00Z",
             "message": {"content": "walk every module"}},
            {"type": "assistant", "timestamp": "2026-09-18T19:00:01Z",
             "message": {"content": [
                 {"type": "text", "text": "Looking at the tree."},
                 {"type": "tool_use", "name": "Grep",
                  "input": {"pattern": "def main", "path": "src"}}]}},
            {"type": "user", "timestamp": "2026-09-18T19:00:02Z",
             "message": {"content": [
                 {"type": "tool_result", "content": "src/a.py:3\nsrc/b.py:9"}]}},
            {"type": "assistant", "timestamp": "2026-09-18T19:00:03Z",
             "message": {"content": [
                 {"type": "text", "text": "Two entry points."}]}},
        ]
        (folder / "agent-abc.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n")
        return self.root / "proj" / ("%s.jsonl" % session)

    def test_prompt_at_start_transcript_and_outcome_at_close(self):
        self._agent_pre("toolu_a9")
        job = self._jobs()[0]
        header = pathlib.Path(job["log_path"]).read_text()
        self.assertIn("# subagent (Explore): map the tree", header)
        self.assertIn("walk every module", header)
        parent = self._transcript("sess-1", "toolu_a9")
        self._fire({"hook_event_name": "PostToolUse", "tool_name": "Agent",
                    "tool_use_id": "toolu_a9", "session_id": "sess-1",
                    "transcript_path": str(parent), "tool_input": {},
                    "tool_response": {"content": [
                        {"type": "text", "text": "Two entry points."}]}})
        text = pathlib.Path(self._jobs()[0]["log_path"]).read_text()
        self.assertIn("Looking at the tree.", text)
        self.assertIn("-> Grep: def main in src", text)
        self.assertIn("| src/a.py:3", text)
        self.assertIn("## done", text)
        # the prompt is in the header once, not repeated by the render
        self.assertEqual(text.count("walk every module"), 1)

    def test_no_transcript_still_closes_the_log(self):
        self._agent_pre("toolu_a8")
        self._post("Agent", "toolu_a8", {"content": "done"})
        text = pathlib.Path(self._jobs()[0]["log_path"]).read_text()
        self.assertIn("left no transcript", text)
        self.assertIn("## done", text)


if __name__ == "__main__":
    unittest.main()
