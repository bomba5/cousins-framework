"""The in-process tool transport: one handler per registry command,
reply as the only chat.db writer, no subprocess anywhere."""
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from cousin_lib import mcp_server
from cousin_lib.runner import tools
from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase


# The shipped registry carries no tracker; docs/mcp.md shows how an
# install adds one. This is that table with every command the CLI has.
TRACKER_TOML = '''
[tools.tracker]
command = "cousin-tracker"
description = "The install's list of work in flight."

[tools.tracker.properties]
title = { type = "string" }
domain = { type = "string", optional = true }
tag = { type = "array", items = "string", optional = true }
id = { type = "integer" }
state = { type = "string", enum = ["open", "active", "blocked", "done", "dropped"] }
notes = { type = "string", optional = true }

[tools.tracker.commands.add]
argv = ["add", "{title}", "--json"]
options = { domain = "--domain", tag = "--tag" }

[tools.tracker.commands.update]
argv = ["update", "{id}"]
options = { title = "--title", domain = "--domain", state = "--state", notes = "--notes" }

[tools.tracker.commands.state]
argv = ["state", "{id}", "{state}"]

[tools.tracker.commands.list]
argv = ["list"]
options = { domain = "--domain", state = "--state" }

[tools.tracker.commands.show]
argv = ["show", "{id}"]

[tools.tracker.commands.delete]
argv = ["delete", "{id}"]
'''


def _install(case):
    """A framework root with one cousin (wren) and one peer (testa)."""
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name); (root / "config").mkdir()
    for slug, port in (("wren", 8100), ("testa", 8101)):
        home = root / "cousins" / slug
        (home / "data").mkdir(parents=True); (home / "memory").mkdir(); (home / "chat").mkdir()
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\nchat_port = %d\n\n[operator]\nname = "Priya"\n'
            '\n[agent]\nrunner = "fake"\n' % (slug, slug.capitalize(), port))
    os.environ["FRAMEWORK_ROOT"] = str(root)
    os.environ["COUSIN_HOME"] = str(root / "cousins" / "wren")
    return root, root / "cousins" / "wren"


def _ctx(case, turn=None, policy=None):
    root, home = _install(case)
    # Task 7 replaces the stand-in with Policy.load(home).
    return tools.ToolContext(home=home, slug="wren", name="Wren", root=root,
                             turn=turn or Turn(),
                             policy=policy or types.SimpleNamespace(outbound_filter=True),
                             stream=None)


def _registry(root):
    # The temporary root carries no config/mcp-registry.toml[.example], so
    # default_registry_path finds nothing there; shipped_default_registry
    # falls back to the checkout's example, the registry every cousin gets.
    return mcp_server.parse_registry(mcp_server.shipped_default_registry(root), "shipped")


def _registry_with_tracker(root):
    text = mcp_server.shipped_default_registry(root) + TRACKER_TOML
    return mcp_server.parse_registry(text, "shipped + tracker")


class TestDefinitions(HermeticCase):
    def test_every_registry_command_has_a_handler(self):
        root, _ = _install(self)
        self.assertEqual(tools.missing_handlers(_registry(root)), [])
        self.assertEqual(tools.missing_handlers(_registry_with_tracker(root)), [])

    def test_definitions_are_the_registry_plus_reply_and_handoff(self):
        root, _ = _install(self)
        names = [d["name"] for d in tools.tool_definitions(_registry_with_tracker(root))]
        for n in ("memory", "send", "job", "schedule", "meeting", "tracker", "reply", "handoff"):
            self.assertIn(n, names)
        reply = next(d for d in tools.tool_definitions(_registry(root)) if d["name"] == "reply")
        self.assertEqual(reply["inputSchema"]["required"], ["text"])


class TestCallNeverSpawns(HermeticCase):
    def test_memory_and_schedule_calls_run_in_process(self):
        ctx = _ctx(self)
        with mock.patch.object(subprocess, "run") as run, mock.patch.object(subprocess, "Popen") as popen:
            text, err = tools.call(ctx, "memory", {"command": "decide", "topic": "t",
                                                   "decision": "d", "reasoning": "w"})
            self.assertFalse(err, text); self.assertIn("Decision logged", text)
            text, err = tools.call(ctx, "memory", {"command": "recall", "keyword": "t", "last": 5})
            self.assertFalse(err); self.assertIn("t: d", text)
            text, err = tools.call(ctx, "schedule", {"command": "add", "when": "in 30m", "prompt": "p"})
            self.assertFalse(err, text); self.assertIn("scheduled #", text)
            text, err = tools.call(ctx, "tracker", {"command": "add", "title": "x", "domain": "d"})
            self.assertFalse(err, text)
            text, err = tools.call(ctx, "job", {"command": "start", "kind": "other", "title": "j"})
            self.assertFalse(err, text)
        self.assertEqual(run.call_count + popen.call_count, 0)

    def test_an_unknown_command_and_a_bad_enum_are_tool_errors(self):
        ctx = _ctx(self)
        text, err = tools.call(ctx, "memory", {"command": "levitate"})
        self.assertTrue(err); self.assertIn("unknown command", text)
        text, err = tools.call(ctx, "memory", {"command": "remember", "topic": "t", "fact": "f",
                                               "level": "gossip"})
        self.assertTrue(err); self.assertIn("level", text)

    def test_a_raising_handler_is_a_tool_error_not_an_exception(self):
        ctx = _ctx(self)
        with mock.patch.dict(tools.HANDLERS["memory"], {"activity": lambda c, a: 1 / 0}):
            text, err = tools.call(ctx, "memory", {"command": "activity", "text": "x"})
        self.assertTrue(err); self.assertIn("ZeroDivisionError", text)


class TestReply(HermeticCase):
    def _rows(self, home):
        # A refused reply never opens the store: no chat.db is no rows.
        if not (home / "data" / "chat.db").exists():
            return []
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            return conn.execute("SELECT chat_user, user, message, type, reply_to_user"
                                " FROM messages ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_reply_with_one_live_thread_writes_the_row_for_that_thread(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        out = tools.reply(ctx, "hello")
        self.assertIn("replied to priya", out)
        rows = self._rows(ctx.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1], rows[0][2], rows[0][3]), ("priya", "Wren", "hello", "wren"))

    def test_reply_without_thread_is_refused_when_two_threads_are_live(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        turn.add({"id": 2, "thread_id": "person:sam", "sender": "Sam"})
        ctx = _ctx(self, turn)
        text, err = tools.call(ctx, "reply", {"text": "hi"})
        self.assertTrue(err); self.assertIn("operator:priya", text); self.assertIn("person:sam", text)
        self.assertEqual(self._rows(ctx.home), [])
        text, err = tools.call(ctx, "reply", {"text": "hi", "thread": "person:sam"})
        self.assertFalse(err, text)
        self.assertEqual(self._rows(ctx.home)[0][0], "sam")

    def test_reply_outside_a_turn_is_refused(self):
        ctx = _ctx(self)
        text, err = tools.call(ctx, "reply", {"text": "hi"})
        self.assertTrue(err); self.assertIn("no turn is live", text)

    def test_reply_to_a_peer_thread_is_refused_with_the_send_hint(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "peer:testa", "sender": "Testa"})
        text, err = tools.call(_ctx(self, turn), "reply", {"text": "hi"})
        self.assertTrue(err); self.assertIn("send", text)

    def test_reply_crosses_the_outbound_filter(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        (ctx.root / "config" / "outbound-filter.json").write_text(json.dumps(
            {"protected": ["mallory"], "terms": []}))
        text, err = tools.call(ctx, "reply", {"text": "ask mallory"})
        self.assertTrue(err); self.assertEqual(self._rows(ctx.home), [])

    def test_send_to_an_operator_is_a_reply_and_to_a_peer_is_a_post(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        text, err = tools.call(ctx, "send", {"to": "Priya", "text": "via send"})
        self.assertFalse(err, text); self.assertEqual(self._rows(ctx.home)[0][2], "via send")
        with mock.patch("cousin_lib.chat.send_message", return_value={"ok": True}) as sm:
            text, err = tools.call(ctx, "send", {"to": "testa", "text": "peer"})
        self.assertFalse(err, text); self.assertEqual(sm.call_args.args[2], "testa")
        text, err = tools.call(ctx, "send", {"to": "nobody", "text": "x"})
        self.assertTrue(err); self.assertIn("testa", text); self.assertIn("Priya", text)


class TestHandoffAndMeeting(HermeticCase):
    def test_handoff_writes_the_manual_file(self):
        ctx = _ctx(self)
        text, err = tools.call(ctx, "handoff", {"text": "carry on from task 3"})
        self.assertFalse(err, text)
        self.assertIn("carry on from task 3", (ctx.home / "data" / "handoff-manual.md").read_text())

    def test_meeting_say_out_of_turn_is_refused(self):
        ctx = _ctx(self)
        from cousin_lib import meetings
        m = meetings.open_meeting("pick a name", ["wren", "testa"], created_by="Priya",
                                  is_alive=lambda s: True, deliver=lambda s, t: True)
        # It is the user's floor: a cousin speaking now is out of turn.
        text, err = tools.call(ctx, "meeting", {"command": "say", "id": m["id"], "text": "me first"})
        self.assertTrue(err); self.assertIn("turn", text.lower())


class TestServerBuild(HermeticCase):
    def test_build_tool_server_refuses_a_registry_command_without_a_handler(self):
        ctx = _ctx(self)
        reg = _registry(ctx.root)
        with mock.patch.dict(tools.HANDLERS["memory"]):
            del tools.HANDLERS["memory"]["recall"]
            from cousin_lib.runner.base import RunnerError
            with self.assertRaises(RunnerError) as cm:
                tools.build_tool_server(ctx, reg)
        self.assertIn("memory.recall", str(cm.exception))

    def test_build_tool_server_names_the_server_cousin(self):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            self.skipTest("claude-agent-sdk not installed")
        ctx = _ctx(self)
        cfg = tools.build_tool_server(ctx, _registry(ctx.root))
        self.assertEqual(cfg["type"], "sdk"); self.assertEqual(cfg["name"], "cousin")


if __name__ == "__main__":
    unittest.main()
