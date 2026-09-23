"""policy.toml -> decisions the PreToolUse hook enforces."""
import asyncio
import pathlib
import tempfile
import unittest

from cousin_lib.runner import hooks, policy
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase


def _home(case, toml=None):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"; (home / "data").mkdir(parents=True)
    if toml is not None:
        (home / "policy.toml").write_text(toml)
    return home


class TestLoad(HermeticCase):
    def test_missing_file_allows_everything_and_says_so(self):
        p = policy.Policy.load(_home(self))
        self.assertEqual(p.decide("Bash", {"command": "rm -rf /"}), ("allow", ""))
        self.assertIn("no policy.toml", p.describe())

    def test_malformed_file_fails_closed_naming_the_key(self):
        with self.assertRaises(policy.PolicyError) as cm:
            policy.Policy.load(_home(self, 'deny_tools = "WebFetch"\n'))
        self.assertIn("deny_tools", str(cm.exception))
        with self.assertRaises(policy.PolicyError) as cm:
            policy.Policy.load(_home(self, "deny_bash_patterns = ['(']\n"))
        self.assertIn("deny_bash_patterns", str(cm.exception))
        with self.assertRaises(policy.PolicyError):
            policy.Policy.load(_home(self, "not toml at all ==\n"))


class TestDecide(HermeticCase):
    def setUp(self):
        super().setUp()
        self.p = policy.Policy.load(_home(self, (
            'deny_tools = ["WebFetch", "mcp__other__*"]\n'
            "deny_bash_patterns = ['\\brm\\s+-rf\\s+/']\n"
            'ask = ["Write"]\noutbound_filter = false\n')))

    def test_denied_tool_exact_and_prefix(self):
        self.assertEqual(self.p.decide("WebFetch", {})[0], "deny")
        self.assertEqual(self.p.decide("mcp__other__thing", {})[0], "deny")
        self.assertEqual(self.p.decide("mcp__cousin__memory", {})[0], "allow")

    def test_bash_pattern(self):
        d, reason = self.p.decide("Bash", {"command": "sudo rm -rf / --no-preserve-root"})
        self.assertEqual(d, "deny"); self.assertIn("deny_bash_patterns", reason)
        self.assertEqual(self.p.decide("Bash", {"command": "ls"})[0], "allow")

    def test_ask_and_outbound_flag(self):
        self.assertEqual(self.p.decide("Write", {})[0], "ask")
        self.assertFalse(self.p.outbound_filter)


class TestHookEnforcement(HermeticCase):
    def test_pre_tool_use_denies_with_reason_and_records_a_policy_event(self):
        home = _home(self, 'deny_tools = ["WebFetch"]\nask = ["Write"]\n')
        stream = EventStream(home, "p")
        machine = StateMachine(on_change=lambda o, n, d: stream.append(
            "state", {"from": o, "to": n, "detail": d}))
        cbs = hooks.callbacks(home, slug="wren", root=home.parent.parent, machine=machine,
                              stream=stream, policy=policy.Policy.load(home))
        base = {"session_id": "s", "transcript_path": "/dev/null", "cwd": str(home)}
        out = asyncio.run(cbs["PreToolUse:policy"]({**base, "hook_event_name": "PreToolUse",
                                             "tool_name": "WebFetch", "tool_input": {},
                                             "tool_use_id": "t"}, "t", {}))
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny"); self.assertIn("deny_tools", spec["permissionDecisionReason"])
        machine.to("running")
        out = asyncio.run(cbs["PreToolUse:policy"]({**base, "hook_event_name": "PreToolUse",
                                             "tool_name": "Write", "tool_input": {"file_path": "x"},
                                             "tool_use_id": "t2"}, "t2", {}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("phase 5", out["hookSpecificOutput"]["permissionDecisionReason"])
        states = [e["payload"]["to"] for e in stream.tail() if e["kind"] == "state"]
        self.assertIn("waiting_permission", states); self.assertEqual(machine.state, "running")
        decisions = [e["payload"]["decision"] for e in stream.tail() if e["kind"] == "policy"]
        self.assertEqual(decisions, ["deny", "ask"])
        out = asyncio.run(cbs["PreToolUse:policy"]({**base, "hook_event_name": "PreToolUse",
                                             "tool_name": "Read", "tool_input": {},
                                             "tool_use_id": "t3"}, "t3", {}))
        self.assertEqual(out, {})


class TestBuildHooks(HermeticCase):
    def test_policy_matcher_is_first_and_no_split_key_leaks(self):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            self.skipTest("claude-agent-sdk not installed")
        home = _home(self, 'deny_tools = ["WebFetch"]\n')
        matchers = hooks.build_hooks(home, slug="wren", root=home.parent.parent,
                                     machine=StateMachine(), stream=EventStream(home, "b"),
                                     policy=policy.Policy.load(home))["PreToolUse"]
        self.assertEqual(len(matchers), 2)
        self.assertIsNone(matchers[0].matcher)
        self.assertEqual(matchers[0].hooks[0].__name__, "PreToolUse:policy")
        self.assertEqual(matchers[1].matcher, hooks.PRE_MATCHER)
        self.assertNotIn("PreToolUse:policy", hooks.build_hooks(
            home, slug="wren", root=home.parent.parent, machine=StateMachine(),
            stream=EventStream(home, "b2"), policy=policy.Policy.load(home)))


if __name__ == "__main__":
    unittest.main()
