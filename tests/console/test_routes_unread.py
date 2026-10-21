"""Unread replies (#380): the read mark lives on the server per console
user, so a chat read in one browser clears its count in every other one,
and the sidebar's badge can say how many."""
import json

from cousin_lib.console import auth
from cousin_lib.server import chat_api
from cousin_lib.server.storage import ChatStore
from tests.console._harness import ConsoleCase

RUNNER = '\n[agent]\nrunner = "fake"\n'


def add(home, *kinds, thread="priya"):
    """One row per kind ("user" is Priya, anything else the cousin's
    type) in the thread; their ids, in order."""
    store = ChatStore(chat_api.db_path(home))
    try:
        return [store.add_message(chat_user=thread, user="Priya" if k == "user" else k,
                                  message="m", msg_type=k)["id"]
                for k in kinds]
    finally:
        store.close()


class TestUnread(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.home = self.cousin("wren", operator="Priya", extra=RUNNER)

    def unread(self):
        status, body = self.get("/api/chat/unread")
        self.assertEqual(status, 200, body)
        return body["unread"]

    def read(self, msg_id, **extra):
        return self.post("/api/chat/read", {"cousin": "wren", "user": "Priya",
                                            "id": msg_id, **extra})

    def test_the_mark_moves_forward_and_counts_what_is_after_it(self):
        ids = add(self.home, "user", "wren", "wren", "wren")
        self.serve()
        self.assertEqual(self.unread(), {"wren": 3})
        status, body = self.read(ids[2])
        self.assertEqual(status, 200, body)
        self.assertEqual((body["mark"], body["unread"]), (ids[2], {"wren": 1}))
        self.assertEqual(self.unread(), {"wren": 1})
        # never back
        self.assertEqual(self.read(ids[1])[1]["mark"], ids[2])
        # never past the thread's newest message
        self.assertEqual(self.read(ids[3] + 50)[1]["mark"], ids[3])
        self.assertEqual(self.unread(), {"wren": 0})
        add(self.home, "wren")
        self.assertEqual(self.unread(), {"wren": 1})

    def test_with_no_mark_the_replies_after_the_last_message_from_a_person_count(self):
        add(self.home, "wren", "wren", "user", "wren")
        self.serve()
        self.assertEqual(self.unread(), {"wren": 1})

    def test_a_thread_nobody_wrote_in_counts_every_reply(self):
        add(self.home, "wren", "wren")
        self.serve()
        self.assertEqual(self.unread(), {"wren": 2})

    def test_archived_replies_do_not_count(self):
        add(self.home, "wren", "wren", "wren")
        store = ChatStore(chat_api.db_path(self.home))
        try:
            store.archive("priya", keep=1)
        finally:
            store.close()
        self.serve()
        self.assertEqual(self.unread(), {"wren": 1})

    def test_only_the_cousins_replies_in_the_operators_thread_count(self):
        add(self.home, "wren", thread="bo")       # another thread
        add(self.home, "heron")                   # not this cousin's type
        add(self.home, "wren")
        self.serve()
        self.assertEqual(self.unread(), {"wren": 1})

    def test_a_mark_past_the_newest_message_is_replaced_on_the_next_read(self):
        """A store rebuilt since the mark was set starts its ids again."""
        ids = add(self.home, "user", "wren")
        prefs = self.root / "data" / "console-prefs" / "_open.json"
        prefs.parent.mkdir(parents=True)
        prefs.write_text(json.dumps({"chat_read": {"wren": {"priya": 900}}}))
        self.serve()
        self.assertEqual(self.unread(), {"wren": 0})
        self.assertEqual(self.read(ids[1])[1]["mark"], ids[1])
        add(self.home, "wren")
        self.assertEqual(self.unread(), {"wren": 1})

    def test_a_cousin_with_no_chat_yet_counts_zero_and_gets_no_store(self):
        self.serve()
        self.assertEqual(self.unread(), {"wren": 0})
        self.assertFalse(chat_api.db_path(self.home).exists())

    def test_a_cousin_the_console_forwards_has_no_count_and_no_mark(self):
        self.cousin("kestrel", operator="Priya")       # a chat port, no runner
        self.serve()
        self.assertNotIn("kestrel", self.unread())
        status, _ = self.post("/api/chat/read", {"cousin": "kestrel",
                                                 "user": "Priya", "id": 1})
        self.assertEqual(status, 404)

    def test_bad_bodies_are_refused(self):
        add(self.home, "wren")
        self.serve()
        for bad in ({"id": -1}, {"id": "3"}, {"id": True}, {"id": None},
                    {"id": 1, "user": ""}, {"id": 1, "user": None}):
            body = {"cousin": "wren", "user": "Priya", **bad}
            self.assertEqual(self.post("/api/chat/read", body)[0], 400, bad)
        self.assertEqual(self.post("/api/chat/read", {"cousin": "Bad Slug",
                                                      "user": "Priya", "id": 1})[0], 400)
        self.assertEqual(self.post("/api/chat/read", {"cousin": "nobody",
                                                      "user": "Priya", "id": 1})[0], 404)
        self.assertFalse((self.root / "data" / "console-prefs").exists())

    def test_a_move_tells_every_page_without_naming_anyone(self):
        ids = add(self.home, "user", "wren")
        server = self.serve()
        seen = []
        server.listeners.append(lambda k, d: seen.append((k, d)))
        self.read(ids[1])
        self.read(ids[1])             # no move, no event
        self.assertEqual([e for e in seen if e[0] == "chat-read"], [("chat-read", {})])

    def test_the_marks_and_the_sidebar_share_one_file_and_keep_each_other(self):
        ids = add(self.home, "user", "wren")
        self.serve()
        self.read(ids[1])
        layout = {"groups": [{"id": "sessions", "name": "Sessions", "collapsed": False}],
                  "assignments": {}}
        self.assertEqual(self.post("/api/prefs/sidebar", {"sidebar": layout})[0], 200)
        stored = json.loads((self.root / "data" / "console-prefs" / "_open.json").read_text())
        self.assertEqual(stored["chat_read"], {"wren": {"priya": ids[1]}})
        self.assertEqual(stored["sidebar"]["groups"][0]["id"], "sessions")
        self.assertEqual(self.unread(), {"wren": 0})

    def test_the_fleet_row_carries_the_newest_reply_id(self):
        ids = add(self.home, "user", "wren", "user")
        self.serve()
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertEqual(row["lastMsgId"], ids[1])
        self.assertGreater(row["lastMsgTs"], 0)


class TestUnreadPerUser(ConsoleCase):
    def setUp(self):
        super().setUp()
        users = auth.Users(self.root / "config" / "console-users.json")
        users.set_password("ana", "correct horse")
        users.set_password("bo", "battery staple")
        self.home = self.cousin("wren", operator="Priya", extra=RUNNER)

    def login(self, user, password):
        self.post("/api/auth/logout")
        self.assertEqual(self.post("/api/auth/login", {"user": user,
                                                       "password": password})[0], 200)

    def test_each_user_reads_for_themselves(self):
        ids = add(self.home, "user", "wren", "wren")
        self.serve()
        self.assertEqual(self.get("/api/chat/unread")[0], 401)
        self.assertEqual(self.post("/api/chat/read", {"cousin": "wren", "user": "Priya",
                                                      "id": ids[2]})[0], 401)
        self.login("ana", "correct horse")
        self.post("/api/chat/read", {"cousin": "wren", "user": "Priya", "id": ids[2]})
        self.assertEqual(self.get("/api/chat/unread")[1]["unread"], {"wren": 0})
        self.login("bo", "battery staple")
        self.assertEqual(self.get("/api/chat/unread")[1]["unread"], {"wren": 2})
        stored = json.loads((self.root / "data" / "console-prefs" / "ana.json").read_text())
        self.assertEqual(stored["chat_read"], {"wren": {"priya": ids[2]}})
        self.assertFalse((self.root / "data" / "console-prefs" / "bo.json").exists())
