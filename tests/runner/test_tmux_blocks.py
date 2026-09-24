"""The content-block mapping the tmux kind reads its transcript with
(phase 11 Task 2, R7): one mapping for both runners, so a turn reads the
same in the console whichever kind ran it. Parity is pinned against the
SDK runner's own `_record` path on the same content; the one declared
difference is a thinking block the interactive CLI keeps without its text
(findings I14b): its length is 0 and it is marked redacted."""
import unittest

from cousin_lib.runner import blocks


class TestDictMapping(unittest.TestCase):
    def test_assistant_blocks(self):
        evs = blocks.assistant_events([
            {"type": "thinking", "thinking": "plan it", "signature": "s"},
            {"type": "text", "text": "done"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
        ])
        self.assertEqual(evs, [
            ("thinking", {"length": 7, "text": "plan it"}),
            ("text", {"text": "done"}),
            ("tool", {"id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}),
        ])

    def test_an_assistant_entry_with_nothing_recordable_is_one_empty_text(self):
        self.assertEqual(blocks.assistant_events([{"type": "server_tool_use"}]),
                         [("text", {"text": ""})])

    def test_a_thinking_block_kept_without_its_text_is_marked_redacted(self):
        self.assertEqual(blocks.assistant_events([{"type": "thinking", "thinking": "", "signature": "x" * 736}]),
                         [("thinking", {"length": 0, "text": "", "redacted": True})])

    def test_thinking_is_bounded_as_on_the_sdk_lane(self):
        [(kind, ev)] = blocks.assistant_events([{"type": "thinking", "thinking": "x" * (blocks.THINKING_CHARS + 3)}])
        self.assertEqual((len(ev["text"]), ev["length"], ev["truncated"]),
                         (blocks.THINKING_CHARS, blocks.THINKING_CHARS + 3, True))

    def test_user_content(self):
        self.assertEqual(blocks.user_events("hello", echo_of=7), [("user", {"text": "hello", "echo_of": 7})])
        self.assertEqual(blocks.user_events([
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False,
             "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]),
            [("tool_result", {"tool_use_id": "toolu_1", "is_error": False, "text": "a\nb"})])
        self.assertEqual(blocks.user_events([{"type": "text", "text": "x" * 2500}])[0][1]["text"], "x" * 2000)


try:
    from claude_agent_sdk import (AssistantMessage, TextBlock, ThinkingBlock, ToolResultBlock,
                                  ToolUseBlock, UserMessage)
except ImportError:                                   # pragma: no cover - CI runs without it
    AssistantMessage = None


@unittest.skipIf(AssistantMessage is None, "claude_agent_sdk is not installed (the sdk extra)")
class TestParityWithTheSdkRunner(unittest.TestCase):
    """The SDK runner's `_record` and blocks.* give the same events for the
    same content (the tmux kind reads dicts, the SDK lane objects)."""

    def test_same_events(self):
        from cousin_lib.delivery import Item
        from cousin_lib.runner.sdk import SdkRunner
        from tests.runner._home import temp_home
        from tests.runner.test_sdk import ScriptedClient, _results, _wait, init_msg, result

        a = AssistantMessage(content=[ThinkingBlock(thinking="plan it", signature="s"),
                                      TextBlock(text="done"),
                                      ToolUseBlock(id="toolu_1", name="Bash", input={"command": "ls"})],
                             model="m")
        u = UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="ok", is_error=False)])
        home = temp_home(self)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, [[init_msg(), a, u, result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:wren", "chat", "x", sender="Wren"))
        self.assertTrue(_wait(lambda: _results(r)))
        sdk = [(e["kind"], e["payload"]) for e in r.events()
               if e["kind"] in ("thinking", "text", "tool", "tool_result")]
        mine = blocks.assistant_events([
            {"type": "thinking", "thinking": "plan it", "signature": "s"},
            {"type": "text", "text": "done"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
        ]) + blocks.user_events([{"type": "tool_result", "tool_use_id": "toolu_1",
                                  "content": "ok", "is_error": False}])
        self.assertEqual(sdk, mine)


if __name__ == "__main__":
    unittest.main()
