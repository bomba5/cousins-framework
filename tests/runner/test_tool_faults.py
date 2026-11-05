"""The tool faults log (runner/tool_faults.py): which calls make a row, what
a row says, and that the sdk kind writes one for a subagent's call too."""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.delivery import Item
from cousin_lib.runner import tool_faults
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result

NO_TOOL = "<tool_use_error>Error: No such tool available: bash</tool_use_error>"
BAD_INPUT = "<tool_use_error>InputValidationError: Monitor failed</tool_use_error>"


class TestClassify(unittest.TestCase):
    def kind(self, name, tool_input, is_error=True, text="boom", seconds=0):
        return tool_faults.classify(name, tool_input, is_error, text, seconds)

    def test_an_ordinary_call_is_none(self):
        self.assertIsNone(self.kind("Read", {"file_path": "/x"}, is_error=False))

    def test_a_tool_that_does_not_exist(self):
        self.assertEqual(self.kind("bash", {"command": "ls"}, text=NO_TOOL), "unknown_tool")

    def test_arguments_the_schema_refused(self):
        self.assertEqual(self.kind("Monitor", {"command": "x"}, text=BAD_INPUT), "bad_input")

    def test_a_tool_search_under_another_name(self):
        query = {"query": "select:Monitor", "max_results": 1}
        self.assertEqual(self.kind("Monitor", query, text=BAD_INPUT), "misnamed")
        self.assertEqual(self.kind("ToolSearch", query), "failed")

    def test_any_other_error_failed(self):
        self.assertEqual(self.kind("Bash", {"command": "false"}), "failed")

    def test_a_foreground_wait_of_a_minute(self):
        wait = {"command": "until [ -f /tmp/x ]; do sleep 20; done"}
        self.assertEqual(self.kind("Bash", wait, is_error=False, seconds=60), "long_wait")
        self.assertIsNone(self.kind("Bash", wait, is_error=False, seconds=59))

    def test_a_wait_cut_at_the_time_limit_is_still_a_wait(self):
        wait = {"command": "while true; do sleep 5; done"}
        self.assertEqual(self.kind("Bash", wait, seconds=600), "long_wait")

    def test_a_background_wait_and_a_long_build_are_not_waits(self):
        wait = {"command": "sleep 300", "run_in_background": True}
        self.assertIsNone(self.kind("Bash", wait, is_error=False, seconds=300))
        self.assertIsNone(self.kind("Bash", {"command": "make"}, is_error=False, seconds=300))

    def test_a_sleep_anywhere_a_command_can_start(self):
        for command in ("cd x && sleep 120", "a; sleep 75; b", "sleep 45",
                        "for i in $(seq 60); do curl x; sleep 5; done",
                        "while true\ndo\n  sleep 5\ndone",
                        "timeout 580 bash -c 'until [ -f /tmp/x ]; do sleep 20; done'"):
            self.assertEqual(self.kind("Bash", {"command": command}, is_error=False, seconds=90),
                             "long_wait", command)

    def test_a_short_sleep_or_the_word_is_not_a_wait(self):
        for command in ("sleep 5 && make test", "git commit -m 'while sleep 5 runs'",
                        "python3 -c 'import time; time.sleep(90)'"):
            self.assertIsNone(self.kind("Bash", {"command": command}, is_error=False, seconds=90),
                              command)

    def test_a_long_command_stays_cheap(self):
        command = "while true; " * 100_000
        started = time.monotonic()
        self.assertIsNone(self.kind("Bash", {"command": command}, is_error=False, seconds=90))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_failed_commands_output_is_not_a_refusal(self):
        for text in ("Exit code 1\nAssertionError: " + NO_TOOL, "log: InputValidationError: x"):
            self.assertEqual(self.kind("Bash", {"command": "make test"}, text=text), "failed")
        self.assertEqual(self.kind("Search", {"query": "select: x"}, text="Exit code 1"), "failed")


class TestLog(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.watch = tool_faults.Watch(self.home, "sdk-abc")

    def test_a_row_says_who_made_the_call(self):
        self.watch.started("t1", "bash", {"command": "ls /tmp"}, model="claude-sub",
                           parent="tu-agent", now=100.0)
        self.watch.finished("t1", True, NO_TOOL, now=103.0)
        row, = tool_faults.read(self.home)
        self.assertEqual((row["kind"], row["tool"], row["agent"], row["parent"], row["model"]),
                         ("unknown_tool", "bash", "subagent", "tu-agent", "claude-sub"))
        self.assertEqual((row["stream"], row["id"], row["seconds"], row["input"], row["keys"]),
                         ("sdk-abc", "t1", 3, "ls /tmp", ["command"]))
        self.assertEqual(row["error"], NO_TOOL)

    def test_an_ordinary_call_leaves_no_row(self):
        self.watch.started("t1", "Read", {"file_path": "/x"})
        self.assertIsNone(self.watch.finished("t1", False, "ok"))
        self.assertFalse(tool_faults.path(self.home).exists())

    def test_a_result_whose_call_was_not_seen_counts_when_it_failed(self):
        self.assertIsNone(self.watch.finished("gone", False, "ok"))
        self.assertEqual(self.watch.finished("gone", True, "boom")["kind"], "failed")

    def test_a_result_whose_call_was_not_seen_keeps_its_agent(self):
        row = self.watch.finished("gone", True, "boom", parent="tu-agent")
        self.assertEqual((row["agent"], row["parent"]), ("subagent", "tu-agent"))

    def test_only_the_owner_reads_the_file(self):
        self.watch.finished("t1", True, "token=abc")
        self.assertEqual(tool_faults.path(self.home).stat().st_mode & 0o777, 0o600)

    def test_the_error_is_cut(self):
        row = self.watch.finished("t1", True, "x" * 5000)
        self.assertEqual(len(row["error"]), tool_faults.ERROR_CHARS)

    def test_the_open_calls_are_bounded(self):
        with mock.patch.object(tool_faults, "MAX_OPEN", 2):
            for n in range(5):
                self.watch.started("t%d" % n, "Read", {})
        self.assertEqual(sorted(self.watch._open), ["t3", "t4"])

    def test_the_file_rolls_and_read_takes_both(self):
        with mock.patch.object(tool_faults, "MAX_BYTES", 1):
            self.watch.finished("t1", True, "one")
            self.watch.finished("t2", True, "two")
            self.watch.finished("t3", True, "three")
        self.assertEqual([r["id"] for r in tool_faults.read(self.home)], ["t2", "t3"])

    def test_a_bad_line_is_skipped(self):
        self.watch.finished("t1", True, "one")
        with open(tool_faults.path(self.home), "a") as fh:
            fh.write("not json\n")
        self.assertEqual(len(tool_faults.read(self.home)), 1)

    def test_a_report_counts_by_kind_and_keeps_to_its_days(self):
        day = 86400.0
        self.watch.finished("old", True, "boom", now=1_000 * day)
        self.watch.finished("t1", True, "boom", now=1_009 * day)
        self.watch.finished("t2", True, NO_TOOL, now=1_009 * day + 1)
        got = tool_faults.report({"wren": self.home}, days=7, now=1_010 * day)
        self.assertEqual(got["totals"], {"wren": {"failed": 1, "unknown_tool": 1}})
        self.assertEqual([r["id"] for r in got["rows"]], ["t2", "t1"])
        self.assertEqual(got["rows"][0]["cousin"], "wren")
        only = tool_faults.report({"wren": self.home}, days=7, kind="failed", limit=1,
                                  now=1_010 * day)
        self.assertEqual(([r["id"] for r in only["rows"]], only["matched"]), (["t1"], 1))
        self.assertEqual(only["totals"], got["totals"])


class TestRunnerWritesTheLog(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)

    def run_turn(self, messages):
        def factory(options):
            return ScriptedClient(options, [messages])
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "go", sender="Priya"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not tool_faults.path(self.home).exists():
            time.sleep(0.02)
        return r

    def test_a_log_that_breaks_does_not_fail_the_turn(self):
        from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
        call = AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "false"})],
                                model="claude-main")
        done = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="exit 1", is_error=True)])
        with mock.patch.object(tool_faults, "append", side_effect=UnicodeEncodeError(
                "utf-8", "x", 0, 1, "surrogate")):
            r = self.run_turn([init_msg(session="s1"), call, done, assistant(text="done"),
                               result(session="s1")])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not [
                    e for e in r.stream.tail() if e.get("kind") == "result"]:
                time.sleep(0.02)
        events = list(r.stream.tail())
        self.assertTrue([e for e in events if e.get("kind") == "result"], events[-5:])
        self.assertTrue([e for e in events if e.get("kind") == "error"
                         and "tool faults" in str(e.get("payload"))])

    def test_a_subagents_failed_call_and_the_cousins_own(self):
        from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
        own = AssistantMessage(content=[ToolUseBlock(id="t-own", name="Bash", input={"command": "false"})],
                               model="claude-main")
        own_done = UserMessage(content=[ToolResultBlock(tool_use_id="t-own", content="exit 1",
                                                        is_error=True)])
        sub = AssistantMessage(content=[ToolUseBlock(
            id="t-sub", name="Monitor", input={"query": "select:Monitor", "max_results": 1})],
            model="claude-sub", parent_tool_use_id="t-agent")
        sub_done = UserMessage(content=[ToolResultBlock(tool_use_id="t-sub", content=BAD_INPUT,
                                                        is_error=True)],
                               parent_tool_use_id="t-agent")
        fine = AssistantMessage(content=[ToolUseBlock(id="t-ok", name="Read", input={"file_path": "/x"})],
                                model="claude-main")
        fine_done = UserMessage(content=[ToolResultBlock(tool_use_id="t-ok", content="text")])
        r = self.run_turn([init_msg(session="s1"), own, own_done, sub, sub_done, fine, fine_done,
                           assistant(text="done"), result(session="s1")])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(tool_faults.read(self.home)) < 2:
            time.sleep(0.02)
        rows = {row["id"]: row for row in tool_faults.read(self.home)}
        self.assertEqual(sorted(rows), ["t-own", "t-sub"])
        self.assertEqual((rows["t-own"]["agent"], rows["t-own"]["model"], rows["t-own"]["kind"]),
                         ("main", "claude-main", "failed"))
        self.assertEqual((rows["t-sub"]["agent"], rows["t-sub"]["model"], rows["t-sub"]["kind"]),
                         ("subagent", "claude-sub", "misnamed"))
        self.assertEqual(rows["t-sub"]["stream"], r.session_id)


if __name__ == "__main__":
    unittest.main()
