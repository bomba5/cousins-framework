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


class TestCommandPatternsOnAnyTool(HermeticCase):
    def test_a_pattern_matches_any_tool_whose_input_carries_a_command(self):
        p = policy.Policy(deny_bash_patterns=(__import__("re").compile(r"\brm\s+-rf\s+/"),))
        for tool in ("Bash", "PowerShell", "Monitor", "mcp__other__shell"):
            decision, reason = p.decide(tool, {"command": "rm -rf / now"})
            self.assertEqual(decision, "deny", tool)
            self.assertIn("deny_bash_patterns", reason)
        self.assertEqual(p.decide("PowerShell", {"command": "Get-ChildItem"})[0], "allow")
        # a command that is not a string is not a command line
        self.assertEqual(p.decide("Monitor", {"command": ["rm", "-rf", "/"]})[0], "allow")
        self.assertEqual(p.decide("Read", {"file_path": "rm -rf /"})[0], "allow")


class TestSubagentReply(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = _home(self)
        self.stream = EventStream(self.home, "sa")
        self.cbs = hooks.callbacks(self.home, slug="wren", root=self.home.parent.parent,
                                   machine=StateMachine(), stream=self.stream,
                                   policy=policy.Policy())
        self.base = {"session_id": "s", "transcript_path": "/dev/null", "cwd": str(self.home),
                     "hook_event_name": "PreToolUse", "tool_name": "mcp__cousin__reply",
                     "tool_use_id": "t"}

    def _gate(self, **kw):
        return asyncio.run(self.cbs["PreToolUse:policy"]({**self.base, **kw}, "t", {}))

    def test_a_subagent_reply_without_a_thread_is_denied(self):
        out = self._gate(agent_id="a-1", agent_type="general-purpose",
                         tool_input={"text": "done"})
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertEqual(spec["permissionDecisionReason"],
                         "a subagent must name the thread it answers")
        events = [e["payload"] for e in self.stream.tail() if e["kind"] == "policy"]
        self.assertEqual(events, [{"tool": "mcp__cousin__reply", "decision": "deny",
                                   "reason": "a subagent must name the thread it answers",
                                   "agent_id": "a-1"}])

    def test_a_blank_thread_is_no_thread(self):
        out = self._gate(agent_id="a-1", tool_input={"text": "done", "thread": "  "})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_subagent_that_names_the_thread_and_the_main_turn_are_allowed(self):
        self.assertEqual(self._gate(agent_id="a-1", tool_input={
            "text": "done", "thread": "operator:priya"}), {})
        self.assertEqual(self._gate(tool_input={"text": "done"}), {})
        self.assertFalse(any(e["kind"] == "policy" for e in self.stream.tail()))


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


class _RaisingPolicy:
    """A policy stub whose decide() always raises, to prove the hook
    fails closed rather than swallowing the error into an allow."""
    def decide(self, tool_name, tool_input):
        raise RuntimeError("boom")


class TestFailClosed(HermeticCase):
    def test_decide_with_none_tool_name_does_not_raise(self):
        # P19: a prefix* deny_tools entry used to crash _named on
        # tool_name=None (None.startswith), which the generic guard()
        # turned into an ALLOW. Must not raise, must not allow blindly.
        p = policy.Policy(deny_tools=("Foo*",))
        self.assertEqual(p.decide(None, {}), ("allow", ""))
        self.assertEqual(policy.Policy().decide(None, {}), ("allow", ""))

    def test_policy_callback_fails_closed_on_exception(self):
        home = _home(self)
        stream = EventStream(home, "fc")
        machine = StateMachine()
        cbs = hooks.callbacks(home, slug="wren", root=home.parent.parent, machine=machine,
                              stream=stream, policy=_RaisingPolicy())
        base = {"session_id": "s", "transcript_path": "/dev/null", "cwd": str(home)}
        out = asyncio.run(cbs["PreToolUse:policy"]({**base, "hook_event_name": "PreToolUse",
                                             "tool_name": "WebFetch", "tool_input": {},
                                             "tool_use_id": "t"}, "t", {}))
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn("policy check failed", spec["permissionDecisionReason"])
        self.assertIn("RuntimeError", spec["permissionDecisionReason"])
        hook_errs = [e for e in stream.tail() if e["kind"] == "hook"]
        self.assertEqual(len(hook_errs), 1)
        self.assertIn("RuntimeError", hook_errs[0]["payload"]["error"])
        policy_events = [e for e in stream.tail() if e["kind"] == "policy"]
        self.assertEqual(len(policy_events), 1)
        self.assertEqual(policy_events[0]["payload"]["decision"], "deny")
        self.assertEqual(policy_events[0]["payload"]["tool"], "WebFetch")


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
