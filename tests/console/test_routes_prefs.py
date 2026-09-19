"""Per-user console preferences: the sidebar groups live on the server,
so another browser (or the phone) sees the same layout."""
import json
import unittest

from cousin_lib.console import auth
from tests.console._harness import ConsoleCase

LAYOUT = {"groups": [{"id": "sessions", "name": "Sessions",
                      "collapsed": False},
                     {"id": "g1", "name": "Work", "collapsed": True}],
          "assignments": {"wren": "g1"}}


class TestSidebarPrefs(ConsoleCase):
    def test_empty_until_saved_then_survives_a_restart(self):
        self.serve()
        self.assertEqual(self.get("/api/prefs/sidebar"),
                         (200, {"sidebar": None}))
        status, body = self.post("/api/prefs/sidebar", {"sidebar": LAYOUT})
        self.assertEqual((status, body["sidebar"]), (200, LAYOUT))
        self.serve()
        self.assertEqual(self.get("/api/prefs/sidebar")[1]["sidebar"],
                         LAYOUT)
        path = self.root / "data" / "console-prefs" / "_open.json"
        self.assertEqual(json.loads(path.read_text())["sidebar"], LAYOUT)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_bad_shapes_are_refused(self):
        self.serve()
        for bad in (None, [], {"groups": []},
                    {"groups": [{"id": "a"}]},
                    {"groups": [{"id": "a", "name": "A"},
                                {"id": "a", "name": "B"}]},
                    {"groups": [{"id": "a", "name": "A"}],
                     "assignments": {"wren": 3}}):
            status, _ = self.post("/api/prefs/sidebar", {"sidebar": bad})
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.get("/api/prefs/sidebar")[1]["sidebar"], None)


class TestSidebarPrefsPerUser(ConsoleCase):
    def setUp(self):
        super().setUp()
        users = auth.Users(self.root / "config" / "console-users.json")
        users.set_password("ana", "correct horse")
        users.set_password("bo", "battery staple")

    def test_each_user_has_their_own_layout(self):
        self.serve()
        self.assertEqual(self.get("/api/prefs/sidebar")[0], 401)
        self.post("/api/auth/login", {"user": "ana",
                                      "password": "correct horse"})
        self.post("/api/prefs/sidebar", {"sidebar": LAYOUT})
        self.post("/api/auth/logout")
        self.post("/api/auth/login", {"user": "bo",
                                      "password": "battery staple"})
        self.assertEqual(self.get("/api/prefs/sidebar")[1]["sidebar"], None)
        self.assertTrue((self.root / "data" / "console-prefs"
                         / "ana.json").is_file())


if __name__ == "__main__":
    unittest.main()
