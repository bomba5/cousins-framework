"""Only `reply` writes chat.db: a turn that produces text and never calls
reply leaves the surface untouched and the stream carrying the text."""
import os
import sqlite3
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("claude-agent-sdk not installed")

from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result  # noqa: E402


class TestOnlyReplyWrites(HermeticCase):
    def test_text_without_reply_never_reaches_chat_db(self):
        home = temp_home(self, runner="sdk")
        (home.parent.parent / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(home.parent.parent)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, [[
            init_msg(), assistant(text="I answer in plain text, no tool"), result()]]))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "hello", sender="Priya"))
        t = time.monotonic()
        while time.monotonic() - t < 5 and not any(e["kind"] == "result" for e in r.events()):
            time.sleep(0.02)
        self.assertIn("plain text", " ".join(e["payload"]["text"] for e in r.events() if e["kind"] == "text"))
        db = home / "data" / "chat.db"
        if db.exists():
            conn = sqlite3.connect(db)
            try:
                n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0)

    def test_reply_through_the_tool_context_writes_exactly_one_row(self):
        home = temp_home(self, runner="sdk")
        (home.parent.parent / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(home.parent.parent)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        r.turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        from cousin_lib.runner import tools
        text, err = tools.call(r.tool_context, "reply", {"text": "via the tool"})
        self.assertFalse(err, text)
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            rows = conn.execute("SELECT chat_user, message FROM messages").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("priya", "via the tool")])


if __name__ == "__main__":
    unittest.main()
