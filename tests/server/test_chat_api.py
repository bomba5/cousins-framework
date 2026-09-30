"""The chat API as library calls (server/chat_api.py): what the console,
the Telegram bridge and cousin-chat call in-process (no cousin runs a
chat server of its own). Hermetic: a temp home, no server, no tmux."""
import base64
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.config import CousinConfig
from cousin_lib.runner.inbox import Inbox
from cousin_lib.server import chat_api
from cousin_lib.server.storage import ChatStore
from tests._hermetic import HermeticCase

_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)).decode()


class ApiCase(HermeticCase):
    runner = None

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        agent = '[agent]\nrunner = "%s"\n' % self.runner if self.runner else ""
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[operator]\nname = "Priya"\n' + agent)
        self.config = CousinConfig.load(self.home)

    def _seed(self, *rows):
        store = ChatStore(chat_api.db_path(self.home))
        try:
            return [store.add_message(chat_user=u.lower(), user=u, message=m, msg_type=t)
                    for u, m, t in rows]
        finally:
            store.close()


class TestReads(ApiCase):
    def test_history_is_the_stores_thread_oldest_first(self):
        self._seed(("Priya", "one", "user"), ("Priya", "two", "user"), ("Sam", "other", "user"))
        out = chat_api.history(self.home, {"user": "Priya"})
        self.assertEqual([m["message"] for m in out["messages"]], ["one", "two"])
        self.assertEqual((out["total"], out["has_more"]), (2, False))
        self.assertEqual(out["messages"][0]["reactions"], [])

    def test_history_refuses_what_the_server_refuses(self):
        with self.assertRaisesRegex(chat_api.BadRequest, "^user is required$"):
            chat_api.history(self.home, {})
        with self.assertRaisesRegex(chat_api.BadRequest, "^limit must be an integer$"):
            chat_api.history(self.home, {"user": "Priya", "limit": "ten"})

    def test_search_wraps_the_hits(self):
        self._seed(("Priya", "the quokka ledger", "user"), ("Priya", "ferns", "user"))
        out = chat_api.search(self.home, {"q": "quokka"})
        self.assertEqual([m["message"] for m in out["messages"]], ["the quokka ledger"])
        with self.assertRaisesRegex(chat_api.BadRequest, "^q is required$"):
            chat_api.search(self.home, {})


class TestWrites(ApiCase):
    def test_archive_keeps_the_newest(self):
        self._seed(("Priya", "a", "user"), ("Priya", "b", "user"), ("Priya", "c", "user"))
        self.assertEqual(chat_api.archive(self.home, {"user": "Priya", "keep": 1}),
                         {"ok": True, "archived": 2})
        with self.assertRaisesRegex(chat_api.BadRequest, "keep must be a non-negative integer"):
            chat_api.archive(self.home, {"user": "Priya", "keep": -1})

    def test_a_tap_notifies_and_a_remove_does_not(self):
        [row] = self._seed(("Wren", "hello", "wren"))
        said = []
        out = chat_api.react(self.home, {"message_id": row["id"], "user": "Priya",
                                         "emoji": "+1", "action": "tap"}, notify=said.append)
        self.assertEqual(out["op"], "added")
        chat_api.react(self.home, {"message_id": row["id"], "user": "Priya",
                                   "emoji": "+1", "action": "remove"}, notify=said.append)
        self.assertEqual(said, ["[fw-reaction] msg-id=%d emoji=+1 user=Priya tap_count=1 op=added"
                                % row["id"]])

    def test_send_stores_delivers_then_hooks(self):
        order = []
        with mock.patch.object(chat_api, "after_inbound_stored",
                               lambda *a: order.append("marker")), \
                mock.patch.object(chat_api.chat_hooks, "on_message",
                                  lambda *a, **k: order.append("hooks")):
            out = chat_api.send(self.config, {"user": "Priya", "message": "hi",
                                              "reply_to": {"id": 1}},
                                deliver=lambda **k: order.append(("deliver", k["message"])))
        self.assertEqual(order, [("deliver", "hi"), "marker", "hooks"])
        [stored] = chat_api.history(self.home, {"user": "Priya"})["messages"]
        self.assertEqual((stored["id"], stored["message"], json.loads(stored["reply_to"])),
                         (out["id"], "hi", {"id": 1}))
        self.assertEqual(set(out), {"ok", "id", "timestamp"})

    def test_send_refuses_an_empty_message(self):
        with self.assertRaisesRegex(chat_api.BadRequest,
                                    "^user and a non-empty message are required$"):
            chat_api.send(self.config, {"user": "Priya", "message": ""})


class TestRunnerCousin(ApiCase):
    """A runner cousin: deliver() puts an inbox row; an image is handed on
    as its file, the path the envelope turns into an image block."""
    runner = "fake"

    def test_make_deliver_queues_the_message_on_the_operators_thread(self):
        out = chat_api.send(self.config, {"user": "Priya", "message": "hello Wren"},
                            deliver=chat_api.make_deliver(self.config))
        [row] = Inbox(self.home).open_rows("chat")
        self.assertEqual((row["thread_id"], row["sender"], row["body"], row["message_id"]),
                         ("operator:Priya", "Priya", "hello Wren", out["id"]))

    def test_an_image_is_delivered_as_its_file(self):
        seen = {}
        out = chat_api.send(self.config, {"user": "Priya", "message": "look",
                                          "image": "data:image/png;base64," + _PNG},
                            deliver=lambda **k: seen.update(k))
        [path] = seen["attachments"]
        self.assertEqual(path, str(self.home / "chat" / "inbound" / ("%d.png" % out["id"])))
        self.assertTrue(pathlib.Path(path).is_file())

    def test_storing_a_message_writes_no_last_user_msg_marker(self):
        # the marker was the legacy pane line's time baseline; nothing
        # reads it any more, so a stored message leaves no file behind
        chat_api.send(self.config, {"user": "Priya", "message": "hello Wren"},
                      deliver=lambda **k: None)
        self.assertFalse((self.home / "data" / ".last-user-msg").exists())

    def test_make_notify_queues_a_reaction(self):
        chat_api.make_notify(self.config)("[fw-reaction] msg-id=1 emoji=+1 user=Priya"
                                          " tap_count=1 op=added")
        [row] = Inbox(self.home).open_rows("reaction")
        self.assertEqual(row["thread_id"], "system")


class TestAttachment(ApiCase):
    def test_an_attachment_is_never_a_read_marker(self):
        # the "[image attached -> Read <path>]" marker was the tmux pane
        # line's; every lane gets the saved file's path
        seen = {}
        out = chat_api.send(self.config, {"user": "Priya", "message": "look",
                                          "image": "data:image/png;base64," + _PNG},
                            deliver=lambda **k: seen.update(k))
        self.assertEqual(seen["attachments"], [str(self.home / "chat" / "inbound"
                                                   / ("%d.png" % out["id"]))])
        self.assertNotIn("context", seen)


if __name__ == "__main__":
    unittest.main()
