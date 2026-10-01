"""The SDK lane's tool calls: in process, no subprocess per call, no
settings-file hooks, and each call recorded once."""
import asyncio
import subprocess
import unittest
from unittest import mock

from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("claude-agent-sdk not installed")


class TestExitCriteria(HermeticCase):
    def test_no_subprocess_per_tool_call_on_the_sdk_lane(self):
        import os
        from cousin_lib.runner.sdk import SdkRunner
        from cousin_lib.runner import tools
        from tests.runner.test_sdk import ScriptedClient
        home = temp_home(self, runner="sdk"); (home.parent.parent / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(home.parent.parent)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        with mock.patch.object(subprocess, "run") as run, mock.patch.object(subprocess, "Popen") as popen:
            for name, args in (("memory", {"command": "activity", "text": "x"}),
                               ("tracker", {"command": "add", "title": "t", "domain": "d"}),
                               ("reply", {"text": "hi"})):
                text, err = tools.call(r.tool_context, name, args)
                self.assertFalse(err, text)
        self.assertEqual(run.call_count + popen.call_count, 0)

    def test_settings_file_hooks_are_absent_and_recording_lands_once(self):
        import os
        from cousin_lib import activity
        from cousin_lib.runner.sdk import SdkRunner
        from tests.runner.test_sdk import ScriptedClient
        home = temp_home(self, runner="sdk"); (home.parent.parent / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(home.parent.parent)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        opts = r.options()
        self.assertEqual(opts.setting_sources, [])
        self.assertFalse((home / ".claude" / "settings.json").exists())
        cb = opts.hooks["PostToolUse"][0].hooks[0]
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_input": {"pattern": "x"},
                   "tool_response": {}, "tool_use_id": "t", "session_id": "s",
                   "transcript_path": "/dev/null", "cwd": str(home)}
        asyncio.run(cb(payload, "t", {}))
        self.assertEqual(activity.activity_path(home).read_text().count("Grep"), 1)


if __name__ == "__main__":
    unittest.main()
