"""Config that changes under a live session (runner/config_watch.py,
Policy.tightened_by, hooks.on_prompt): announced at the next prompt, a
policy edit tightens at once and never loosens."""
import asyncio
import pathlib
import tempfile
import unittest

from cousin_lib.runner import config_watch, hooks
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase


class TestTightenedBy(unittest.TestCase):
    def test_additions_apply_and_removals_wait(self):
        start = Policy.parse('deny_tools = ["WebFetch"]\ndeny_bash_patterns = ["shutdown"]\n')
        edit = Policy.parse('deny_tools = ["Write"]\ndeny_bash_patterns = ["git push"]\n')
        live, added, removed, skipped = start.tightened_by(edit)
        self.assertEqual(set(live.deny_tools), {"WebFetch", "Write"})
        self.assertEqual({p.pattern for p in live.deny_bash_patterns}, {"shutdown", "git push"})
        self.assertEqual(added, ["deny_tools: Write", "deny_bash_patterns: git push"])
        self.assertEqual(removed, ["deny_tools: WebFetch", "deny_bash_patterns: shutdown"])
        self.assertEqual(skipped, [])
        self.assertEqual(live.decide("WebFetch", {})[0], "deny")     # removal not in force

    def test_the_outbound_filter_is_never_switched_off_live(self):
        live, _, removed, _ = Policy().tightened_by(Policy.parse("outbound_filter = false\n"))
        self.assertTrue(live.outbound_filter)
        self.assertEqual(removed, ["outbound_filter: true"])

    def test_an_entry_that_would_deny_the_handoff_is_left_out(self):
        live, added, _, skipped = Policy().tightened_by(
            Policy.parse('deny_tools = ["mcp__cousin__*", "Write"]\n'))
        self.assertEqual(live.deny_tools, ("Write",))
        self.assertEqual(skipped, ["deny_tools: mcp__cousin__*"])
        self.assertEqual(added, ["deny_tools: Write"])


class HomeCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.law = self.root / "config" / "law.md"
        self.law.write_text("1. Be kind.\n2. Be brief.\n")
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "memory"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')


class TestNote(HomeCase):
    def test_nothing_changed_is_no_note(self):
        watch = config_watch.ConfigWatch(self.home, self.root)
        self.assertEqual(watch.check(), [])
        self.assertEqual(config_watch.note([], Policy(), Policy.parse)[0], "")

    def test_a_law_edit_carries_its_diff_once(self):
        watch = config_watch.ConfigWatch(self.home, self.root)
        self.law.write_text("1. Be kind.\n2. Be brief, and say I don't know.\n")
        text, _ = config_watch.note(watch.check(), Policy(), Policy.parse)
        self.assertIn(config_watch.NOTE_HEAD, text)
        self.assertIn("+2. Be brief, and say I don't know.", text)
        self.assertIn("the new text below wins", text)
        self.assertEqual(watch.check(), [])                 # announced once

    def test_cousin_toml_and_a_broken_policy(self):
        watch = config_watch.ConfigWatch(self.home, self.root)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n[agent]\nmodel = "x"\n')
        (self.home / "policy.toml").write_text("deny_tools = [\n")
        text, live = config_watch.note(watch.check(), Policy(), Policy.parse)
        self.assertIn("cousin.toml changed. It is read when the runner starts", text)
        self.assertIn("policy.toml no longer loads", text)
        self.assertEqual(live, Policy())


class TestLiveKeys(HomeCase):
    def test_a_dreaming_change_says_it_needs_no_restart(self):
        watch = config_watch.ConfigWatch(self.home, self.root)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[agent]\ndreaming = "nightly"\n')
        text, _ = config_watch.note(watch.check(), Policy(), Policy.parse)
        self.assertIn("[agent] dreaming = 'nightly'", text)
        self.assertIn("apply without a restart", text)
        # a key the runner reads at start still says restart
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[agent]\ndreaming = "nightly"\nmodel = "x"\n')
        text, _ = config_watch.note(watch.check(), Policy(), Policy.parse)
        self.assertIn("a restart applies", text)


class TestHooksApplyIt(HomeCase):
    def test_a_policy_edit_denies_at_the_next_prompt(self):
        stream = EventStream(self.home, "cfg-test")
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root,
                              machine=StateMachine(on_change=lambda *a: None), stream=stream,
                              recall=lambda body: (None, 0), policy=Policy(),
                              watch=config_watch.ConfigWatch(self.home, self.root))
        call = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"}}
        run = asyncio.run
        self.assertEqual(run(cbs["PreToolUse:policy"](call, "t", {})), {})
        (self.home / "policy.toml").write_text('deny_bash_patterns = ["git\\\\s+push"]\n')
        out = run(cbs["UserPromptSubmit"]({"hook_event_name": "UserPromptSubmit",
                                           "prompt": "go on"}, None, {}))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("In force now: deny_bash_patterns", ctx)
        denied = run(cbs["PreToolUse:policy"](call, "t", {}))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        # and a later edit that drops it does not loosen the live session
        (self.home / "policy.toml").write_text("")
        out = run(cbs["UserPromptSubmit"]({"hook_event_name": "UserPromptSubmit",
                                           "prompt": "again"}, None, {}))
        self.assertIn("still enforced until the next runner start",
                      out["hookSpecificOutput"]["additionalContext"])
        denied = run(cbs["PreToolUse:policy"](call, "t", {}))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")


class TestTheRunnerKeepsIt(HomeCase):
    """A rollover or a reconnect builds new hooks (options() per connect):
    they start from the tightened policy, the watch keeps its baseline, and
    the tools read the outbound filter from the same policy."""

    def setUp(self):
        super().setUp()
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            self.skipTest("claude-agent-sdk not installed")
        import os
        from unittest import mock
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                         "COUSIN_HOME": str(self.home)})
        p.start(); self.addCleanup(p.stop)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[agent]\nrunner = "sdk"\n')
        (self.home / "policy.toml").write_text("outbound_filter = false\n")

    def prompt_and_gate(self, opts, command):
        cbs = {ev: [h for m in ms for h in m.hooks] for ev, ms in opts.hooks.items()}
        run = asyncio.run
        for cb in cbs["UserPromptSubmit"]:
            run(cb({"hook_event_name": "UserPromptSubmit", "prompt": "next"}, None, {}))
        call = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": command}}
        outs = [run(cb(call, "t", {})) for cb in cbs["PreToolUse"]]
        return any((o or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                   for o in outs)

    def test_a_live_tightening_survives_a_new_connect(self):
        from cousin_lib.runner.sdk import SdkRunner
        r = SdkRunner(self.home)
        first = r.options()
        self.assertFalse(r.tool_context.policy.outbound_filter)
        (self.home / "policy.toml").write_text(
            'outbound_filter = true\ndeny_bash_patterns = ["git\\\\s+push"]\n')
        self.assertTrue(self.prompt_and_gate(first, "git push origin main"))
        # the tools read the tightened policy: the outbound filter is on
        self.assertTrue(r.tool_context.policy.outbound_filter)
        self.assertIs(r.policy, r.tool_context.policy)
        # a rollover / reconnect: new hooks, same live policy, no new note
        second = r.options()
        self.assertTrue(self.prompt_and_gate(second, "git push origin main"))
        self.assertEqual(r.config_watch.check(), [])
