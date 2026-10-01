"""A runner cousin's fleet row: liveness from
the runner's lock, its state and its declared-unsupported items from its
own stream (runner/status.py), the last reply from chat.db; never a chat
server call, never tmux."""
import threading
import unittest

from cousin_lib.runner import main
from cousin_lib.runner.stream import EventStream
from cousin_lib.server import chat_api
from cousin_lib.server.storage import ChatStore
from tests.console._harness import ConsoleCase

RUNNER = '\n[agent]\nrunner = "fake"\n'


class TestRunnerRow(ConsoleCase):
    def _row(self):
        return self.get("/api/cousins")[1]["cousins"][0]

    def test_a_stopped_runner_cousin_is_stopped_with_its_last_state(self):
        home = self.cousin("wren", operator="Priya", extra=RUNNER)
        stream = EventStream(home, "fake-1")
        stream.append("runner", {"kind": "fake", "pid": 4242, "unsupported": ["midturn_fold"]})
        stream.append("state", {"from": "idle", "to": "stopped", "detail": ""})
        self.serve()
        row = self._row()
        self.assertEqual((row["status"], row["chat"], row["pid"]), ("stopped", "console", None))
        self.assertEqual(row["runner"]["state"], "stopped")
        self.assertEqual(row["runner"]["unsupported"], ["midturn_fold"])
        self.assertFalse(row["runner"]["alive"])
        self.assertFalse(self.tmux_log.exists() and "has-session" in self.tmux_log.read_text())

    def test_a_running_runner_cousin_is_running_and_active_mid_turn(self):
        home = self.cousin("wren", operator="Priya", extra=RUNNER)
        stream = EventStream(home, "fake-2")
        stream.append("runner", {"kind": "fake", "pid": 4243, "unsupported": []})
        stream.append("state", {"from": "idle", "to": "running", "detail": "turn"})
        held, release = threading.Event(), threading.Event()

        def hold():
            with main.hold_lock(home):
                held.set()
                release.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        self.serve()
        row = self._row()
        self.assertEqual((row["status"], row["active"], row["pid"]), ("running", True, 4243))
        self.assertEqual(row["runner"]["kind"], "fake")

    def test_the_last_reply_time_comes_from_chat_db(self):
        home = self.cousin("wren", operator="Priya", extra=RUNNER)
        store = ChatStore(chat_api.db_path(home))
        try:
            store.add_message(chat_user="priya", user="Priya", message="hi", msg_type="user")
            store.add_message(chat_user="priya", user="Wren", message="hello", msg_type="wren")
        finally:
            store.close()
        self.serve()
        self.assertGreater(self._row()["lastMsgTs"], 0)

    def test_the_fleet_read_creates_no_chat_db(self):
        """A GET has no side effect: a runner cousin with no chat yet keeps
        having none."""
        home = self.cousin("wren", operator="Priya", extra=RUNNER)
        self.serve()
        self.assertEqual(self._row()["lastMsgTs"], 0)
        self.assertFalse((home / "data" / "chat.db").exists())

    def test_a_tmux_cousin_row_carries_no_runner_object(self):
        self.cousin("wren")
        self.serve()
        row = self._row()
        self.assertIsNone(row["runner"])
        self.assertNotEqual(row["chat"], "console")


if __name__ == "__main__":
    unittest.main()
