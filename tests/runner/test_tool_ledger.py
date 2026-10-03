"""The live turn's tool-call ledger (runner/tool_ledger.py): what a restart
must not repeat."""
import tempfile
import pathlib
import unittest

from cousin_lib.runner import tool_ledger


class TestToolLedger(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)

    def test_states_follow_start_and_result(self):
        tool_ledger.started(self.home, "a", "Bash", {"command": "git push"})
        tool_ledger.started(self.home, "b", "Write", {"file_path": "/etc/x"})
        tool_ledger.started(self.home, "c", "mcp__cousin__send", {"to": "kestrel", "text": "hi"})
        tool_ledger.finished(self.home, "a")
        tool_ledger.finished(self.home, "b", error=True)
        self.assertEqual([(c["tool"], c["state"]) for c in tool_ledger.calls(self.home)],
                         [("Bash", "done"), ("Write", "error"), ("mcp__cousin__send", "open")])
        text = tool_ledger.lines(self.home, comes_again=True)
        self.assertIn("`git push` (finished)", text)
        self.assertIn("`/etc/x` (failed)", text)
        self.assertIn("`hi` (STARTED, NO RESULT", text)

    def test_read_only_tools_and_their_results_leave_no_line(self):
        tool_ledger.started(self.home, "r", "Read", {"file_path": "/x"})
        tool_ledger.finished(self.home, "r")
        self.assertEqual(tool_ledger.calls(self.home), [])
        self.assertEqual(tool_ledger.lines(self.home, comes_again=True), "")

    def test_long_inputs_are_cut_and_only_the_last_calls_are_listed(self):
        for i in range(tool_ledger.MAX_LINES + 3):
            tool_ledger.started(self.home, str(i), "Bash", {"command": "x" * 500 + str(i)})
        text = tool_ledger.lines(self.home, comes_again=True)
        self.assertIn("(3 earlier calls not shown.)", text)
        self.assertEqual(text.count("\n- "), tool_ledger.MAX_LINES)
        self.assertTrue(all(len(line) < tool_ledger.SUMMARY_CHARS + 80
                            for line in text.splitlines()[1:]))

    def test_clear_and_a_torn_line(self):
        tool_ledger.started(self.home, "a", "Bash", {"command": "ls -la"})
        with open(self.home / "data" / "turn-tools.jsonl", "a") as f:
            f.write('{"id": "b", "event": "sta')            # a writer that died mid-line
        self.assertEqual(len(tool_ledger.calls(self.home)), 1)
        tool_ledger.clear(self.home)
        tool_ledger.clear(self.home)                        # twice is fine
        self.assertEqual(tool_ledger.calls(self.home), [])

    def test_begin_records_the_turn_and_the_wording_follows_the_message(self):
        tool_ledger.started(self.home, "old", "Bash", {"command": "an older turn"})
        tool_ledger.begin(self.home, [{"id": 7, "body": "push and tell kestrel"}])
        tool_ledger.started(self.home, "a", "Bash", {"command": "git push"})
        self.assertEqual(tool_ledger.turn(self.home)["ids"], [7])
        self.assertEqual([c["summary"] for c in tool_ledger.calls(self.home)], ["git push"])
        again = tool_ledger.lines(self.home, comes_again=True)
        self.assertIn("delivered again after this line", again)
        self.assertNotIn("push and tell kestrel", again)
        closed = tool_ledger.lines(self.home, comes_again=False)
        self.assertIn("is not delivered again", closed)
        self.assertIn('The message was: "push and tell kestrel"', closed)
