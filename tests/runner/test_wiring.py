"""SdkRunner carries the tool server, the hooks and the policy; nothing
here spawns a process per tool call."""
import asyncio
import os
import subprocess
import time
import unittest
from unittest import mock

from cousin_lib import activity
from cousin_lib.delivery import Item
from cousin_lib.runner import hooks
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("claude-agent-sdk not installed")

from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result  # noqa: E402


def _wait(pred, timeout=5.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class PromptHookClient(ScriptedClient):
    """Fires the options' UserPromptSubmit hook for every message INSIDE
    query(), before the echo and before query() returns: the earliest
    the real CLI could, so the runner has not yet seen the echo."""

    async def query(self, prompt, session_id="default"):
        messages = [prompt] if isinstance(prompt, str) else [m async for m in prompt]
        cb = self.options.hooks["UserPromptSubmit"][0].hooks[0]
        for message in messages:
            text = message["message"]["content"][0]["text"]
            await cb({"hook_event_name": "UserPromptSubmit", "prompt": text,
                      "session_id": "s", "transcript_path": "/dev/null", "cwd": "."}, None, {})

        async def again():
            for m in messages:
                yield m
        await super().query(again(), session_id)


class TestWiring(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="sdk")
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(root)

    def _runner(self, scripts=(), client=ScriptedClient):
        r = SdkRunner(self.home, client_factory=lambda o: client(o, list(scripts)))
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def test_options_carry_the_cousin_tool_server_and_the_hooks(self):
        opts = self._runner().options()
        self.assertEqual(opts.mcp_servers["cousin"]["name"], "cousin")
        for ev in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "PreCompact",
                   "PermissionRequest"):
            self.assertIn(ev, opts.hooks)
        self.assertEqual(opts.setting_sources, [])       # no settings-file hooks on this lane
        self.assertEqual(opts.permission_mode, "bypassPermissions")
        self.assertIn("replay-user-messages", opts.extra_args)

    def test_the_policy_hook_runs_first_on_pre_tool_use(self):
        (self.home / "policy.toml").write_text('deny_tools = ["WebFetch"]\n')
        r = self._runner()
        pre = r.options().hooks["PreToolUse"]
        self.assertIsNone(pre[0].matcher)
        out = asyncio.run(pre[0].hooks[0]({"hook_event_name": "PreToolUse",
                                           "tool_name": "WebFetch", "tool_input": {}},
                                          "t", {}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_bare_root_falls_back_to_the_shipped_registry_and_says_so(self):
        r = self._runner()
        r.options()
        self.assertIn("memory", r.tool_context.registry["tools"])
        said = [e["payload"] for e in r.events()
                if e["kind"] == "policy" and e["payload"].get("registry") == "shipped default"]
        self.assertTrue(said and said[0]["fallback"], said)

    def test_a_tool_call_through_the_server_spawns_nothing(self):
        r = self._runner()
        cfg = r.options().mcp_servers["cousin"]
        server = cfg["instance"]
        from cousin_lib.runner import tools
        with mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(subprocess, "Popen") as popen:
            text, err = tools.call(r.tool_context, "memory",
                                   {"command": "activity", "text": "wired"})
        self.assertFalse(err, text)
        self.assertEqual(run.call_count + popen.call_count, 0)
        self.assertEqual(server.name, "cousin")

    def test_an_mcp_tool_use_is_recorded_as_tool_call_in_the_stream(self):
        from claude_agent_sdk import AssistantMessage, ToolUseBlock
        r = self._runner([[init_msg(), AssistantMessage(content=[ToolUseBlock(
            id="tu", name="mcp__cousin__memory", input={"command": "recall"})], model="m"),
            assistant(text="ok"), result()]])
        r.start()
        r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        tools_seen = [e["payload"]["name"] for e in r.events() if e["kind"] == "tool"]
        self.assertIn("mcp__cousin__memory", tools_seen)

    def test_one_activity_line_per_tool_call_on_the_sdk_lane(self):
        r = self._runner()
        cb = r.options().hooks["PostToolUse"][0].hooks[0]
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Read",
                   "tool_input": {"file_path": "x"}, "tool_response": {},
                   "tool_use_id": "t", "session_id": "s",
                   "transcript_path": "/dev/null", "cwd": str(self.home)}
        asyncio.run(cb(payload, "t", {}))
        self.assertEqual(activity.activity_path(self.home).read_text().count("Read"), 1)


class TestBodyForPrompt(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="sdk")
        (self.home.parent.parent / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(self.home.parent.parent)
        self.searched = []
        patch = mock.patch.object(hooks, "default_recall",
                                  lambda home: lambda body: (self.searched.append(body),
                                                             (None, 0))[1])
        patch.start()
        self.addCleanup(patch.stop)

    def _runner(self):
        r = SdkRunner(self.home, client_factory=lambda o: PromptHookClient(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def test_the_hook_searches_the_row_it_fired_for_before_the_echo(self):
        r = self._runner()
        r.start()
        r.enqueue(Item("operator:priya", "chat", "where is the boat", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        self.assertEqual(self.searched, ["where is the boat"])

    def test_a_peer_row_is_not_recalled_for(self):
        r = self._runner()
        r.start()
        r.enqueue(Item("peer:toki", "chat", "status of the boat", sender="Toki"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events())))
        self.assertEqual(self.searched, [""])

    def test_the_matching_row_wins_over_the_newest(self):
        r = self._runner()
        older = {"id": 1, "thread_id": "operator:priya", "body": "older"}
        newer = {"id": 2, "thread_id": "person:sam", "body": "newer"}
        r._sent = [(older, "[operator:priya] chat from Priya\n\nolder"),
                   (newer, "[person:sam] chat from Sam\n\nnewer")]
        self.assertEqual(r._body_for_prompt("[operator:priya] chat from Priya\n\nolder"),
                         "older")
        self.assertEqual(r._body_for_prompt("[person:sam] chat from Sam\n\nnewer"), "newer")
        self.assertEqual(r._body_for_prompt("something else"), "")


if __name__ == "__main__":
    unittest.main()
