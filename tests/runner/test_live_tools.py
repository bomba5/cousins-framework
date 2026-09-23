"""Live, opt in with COUSIN_LIVE_SDK=1: one real turn on the SDK lane
calls mcp__cousin__memory and mcp__cousin__reply in-process; another
has policy.toml deny mcp__cousin__handoff and checks the CLI refuses it
while the turn goes on to reply. Small model turns on the machine's
lane. At most two runs per attempt."""
import os
import sqlite3
import subprocess
import unittest
from unittest import mock

from cousin_lib import delivery
from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _evidence(r):
    """The session_init payload (tools, mcp_servers, apiKeySource, model)
    and the whole stream's kind sequence, so a failing assertion still
    carries the evidence: whether the tools were offered at all is a
    different finding than the model not calling them."""
    events = list(r.events())
    inits = [e["payload"] for e in events if e["kind"] == "session_init"]
    kinds = [e["kind"] for e in events]
    return "session_init=%r kinds=%r" % (inits, kinds)


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveTools(HermeticCase):
    def test_memory_and_reply_are_called_in_process(self):
        home = temp_home(self, runner="sdk")
        root = home.parent.parent; (root / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(root)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Priya"\n\n[agent]\nrunner = "sdk"\n')
        r = SdkRunner(home, model="claude-haiku-4-5-20251001", idle_timeout_s=120)
        self.addCleanup(lambda: r.stop(timeout=30))
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess.run")), \
                mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
            r.start()
            out = delivery.deliver(home, Item(
                "operator:priya", "chat",
                "Call the tool mcp__cousin__memory with command=activity and"
                " text='live tools'. Then call the tool mcp__cousin__reply with"
                " text='done'. Use only those two tools, then stop.",
                sender="Priya"), wait=True, timeout=180)
        evidence = _evidence(r)
        self.assertEqual(out, delivery.DELIVERED, evidence)
        # asyncio's unix subprocess transport calls subprocess.Popen under the
        # hood (asyncio/unix_events.py), so both of the SDK's own spawns show
        # up here: its version check (`claude -v`, ClaudeSDKClient.connect(),
        # skippable with CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK) and the CLI
        # itself. Neither is per-tool-call: both happen before either tool
        # runs. The point stays zero processes per tool call, not zero
        # processes total, so the ceiling is 2, not 1.
        self.assertLessEqual(popen.call_count, 2, (popen.call_args_list, evidence))
        calls = [e["payload"] for e in r.events() if e["kind"] == "tool_call"]
        self.assertIn(("memory", "activity"), [(c["tool"], c["command"]) for c in calls], evidence)
        self.assertIn("reply", [c["tool"] for c in calls], evidence)
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            rows = conn.execute("SELECT chat_user, message FROM messages").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, evidence); self.assertEqual(rows[0][0], "priya", evidence)
        self.assertIn("live tools", (home / "data" / "last-activity.txt").read_text(), evidence)


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLivePolicy(HermeticCase):
    def test_a_denied_tool_is_refused_by_the_cli_and_the_turn_goes_on(self):
        home = temp_home(self, runner="sdk")
        root = home.parent.parent; (root / "config").mkdir(exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Priya"\n\n[agent]\nrunner = "sdk"\n')
        (home / "policy.toml").write_text('deny_tools = ["mcp__cousin__handoff"]\n')
        r = SdkRunner(home, model="claude-haiku-4-5-20251001", idle_timeout_s=120)
        self.addCleanup(lambda: r.stop(timeout=30))
        r.start()
        out = delivery.deliver(home, Item(
            "operator:priya", "chat",
            "Call the tool mcp__cousin__handoff with text='x'. Whatever it returns, then call"
            " the tool mcp__cousin__reply with text='done'. Use only those two tools, then stop.",
            sender="Priya"), wait=True, timeout=180)
        evidence = _evidence(r)
        self.assertEqual(out, delivery.DELIVERED, evidence)
        denies = [e["payload"] for e in r.events() if e["kind"] == "policy"
                  and e["payload"].get("decision") == "deny"]
        self.assertIn("mcp__cousin__handoff", [d.get("tool") for d in denies], evidence)
        self.assertFalse((home / "data" / "handoff-manual.md").exists(), evidence)
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            rows = conn.execute("SELECT chat_user, message FROM messages").fetchall()
        finally:
            conn.close()
        self.assertIn(("priya", "done"), rows, evidence)


if __name__ == "__main__":
    unittest.main()
