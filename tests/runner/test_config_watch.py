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
