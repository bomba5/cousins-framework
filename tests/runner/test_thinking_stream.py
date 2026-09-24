"""The reasoning stream carries the cousin's thinking (spec, "Features as
in-process tools": "The cousin's plain text, thinking and tool calls go to
the reasoning stream"; master plan phase 5 exit: thinking visible live in
the console and in cousin-watch). Recorded since phase 2 as its length
only; the text, bounded like a tool result's, is what a viewer can show."""
import unittest

try:
    from claude_agent_sdk import AssistantMessage, ThinkingBlock
except ImportError:                                   # pragma: no cover - CI runs without it
    raise unittest.SkipTest("claude_agent_sdk is not installed (the sdk extra)")

from cousin_lib.delivery import Item
from cousin_lib.runner import sdk as sdk_module
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, _results, _wait, init_msg, result


class TestThinkingText(HermeticCase):
    def _thinking_events(self, text):
        home = temp_home(self)
        msg = AssistantMessage(content=[ThinkingBlock(thinking=text, signature="s")], model="m")
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, [[init_msg(), msg,
                                                                          result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        self.assertTrue(_wait(lambda: _results(r)))
        return [e["payload"] for e in r.events() if e["kind"] == "thinking"]

    def test_the_thinking_text_is_in_the_stream(self):
        [ev] = self._thinking_events("the ledger closes on Monday, so")
        self.assertEqual(ev, {"length": 31, "text": "the ledger closes on Monday, so"})

    def test_long_thinking_is_bounded_and_says_so(self):
        [ev] = self._thinking_events("x" * (sdk_module.THINKING_CHARS + 10))
        self.assertEqual(len(ev["text"]), sdk_module.THINKING_CHARS)
        self.assertEqual((ev["length"], ev["truncated"]), (sdk_module.THINKING_CHARS + 10, True))


if __name__ == "__main__":
    unittest.main()
