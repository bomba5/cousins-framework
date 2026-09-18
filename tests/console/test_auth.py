"""The auth model: PBKDF2 users in config/console-users.json, a
session cookie, configured-means-enforced with no address bypass,
not-configured-means-open-and-said."""
import json
import os
import stat
import unittest
from unittest import mock

from cousin_lib.console import auth
from tests.console._harness import ConsoleCase


class TestUsersFile(ConsoleCase):
    def test_set_password_writes_pbkdf2_atomically_mode_0600(self):
        users = auth.Users(self.root / "config" / "console-users.json")
        self.assertFalse(users.configured())
        users.set_password("ana", "correct horse")
        data = json.loads(users.path.read_text())
        self.assertEqual(set(data["ana"]), {"salt", "hash", "iterations"})
        self.assertEqual(data["ana"]["iterations"], auth.ITERATIONS)
        self.assertEqual(len(bytes.fromhex(data["ana"]["salt"])), 16)
        self.assertEqual(stat.S_IMODE(users.path.stat().st_mode), 0o600)
        self.assertTrue(users.configured())
        self.assertTrue(users.verify("ana", "correct horse"))
        self.assertFalse(users.verify("ana", "wrong"))
        self.assertFalse(users.verify("nobody", "correct horse"))
        self.assertFalse(list(users.path.parent.glob("*.tmp")))

    def test_reset_keeps_one_entry_and_changes_the_salt(self):
        users = auth.Users(self.root / "config" / "console-users.json")
        users.set_password("ana", "one")
        salt = json.loads(users.path.read_text())["ana"]["salt"]
        users.set_password("ana", "two")
        data = json.loads(users.path.read_text())
        self.assertEqual(list(data), ["ana"])
        self.assertNotEqual(data["ana"]["salt"], salt)
        self.assertTrue(users.verify("ana", "two"))

    def test_sessions_expire_when_idle(self):
        sessions = auth.Sessions(idle_seconds=100)
        token = sessions.create("ana", now=1000)
        self.assertEqual(sessions.lookup(token, now=1050), "ana")
        self.assertEqual(sessions.lookup(token, now=1149), "ana")
        self.assertIsNone(sessions.lookup(token, now=1300))
        self.assertIsNone(sessions.lookup("nope", now=0))


class TestNotConfigured(ConsoleCase):
    def test_open_and_said(self):
        self.serve()
        status, body = self.get("/api/auth/me")
        self.assertEqual(body, {"user": None, "configured": False,
                                "users": []})
        self.assertEqual(self.get("/api/cousins")[0], 200)
        status, body = self.post("/api/auth/login",
                                 {"user": "a", "password": "b"})
        self.assertEqual((status, body["error"]),
                         (409, "auth not configured"))


class TestConfigured(ConsoleCase):
    def setUp(self):
        super().setUp()
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")

    def test_every_api_route_needs_a_session_except_login_and_me(self):
        self.cousin("wren")
        self.serve()
        self.assertEqual(self.get("/api/cousins")[0], 401)
        self.assertEqual(self.get("/api/jobs")[0], 401)
        self.assertEqual(self.post("/api/cousins/wren/start")[0], 401)
        self.assertEqual(self.delete("/api/cousins/wren")[0], 401)
        self.assertEqual(self.get("/api/auth/me")[0], 200)
        self.assertEqual(self.get("/api/auth/me")[1]["configured"], True)
        self.assertEqual(self.get("/api/auth/me")[1]["users"], [])
        # No loopback bypass: the client IS loopback here and got 401.

    def test_login_sets_an_httponly_cookie_and_me_reports_the_user(self):
        self.serve()
        status, body = self.post("/api/auth/login",
                                 {"user": "ana", "password": "wrong"})
        self.assertEqual((status, body), (401, {"ok": False,
                                                "error": "bad credentials"}))
        status, body = self.post("/api/auth/login", {"user": "ana"})
        self.assertEqual(status, 400)
        status, headers, raw = self.request(
            "POST", "/api/auth/login",
            {"user": "ana", "password": "correct horse"}, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "user": "ana"})
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("console_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)
        status, body = self.get("/api/auth/me")
        self.assertEqual(body, {"user": "ana", "configured": True,
                                "users": ["ana"]})
        self.assertEqual(self.get("/api/cousins")[0], 200)

    def test_logout_forgets_the_session(self):
        self.serve()
        self.post("/api/auth/login", {"user": "ana",
                                      "password": "correct horse"})
        status, body = self.post("/api/auth/logout")
        self.assertEqual(body, {"ok": True})
        self.assertEqual(self.get("/api/cousins")[0], 401)

    def test_change_password(self):
        self.serve()
        status, _ = self.post("/api/auth/change-password",
                              {"old_password": "x", "new_password": "y" * 8})
        self.assertEqual(status, 401)
        self.post("/api/auth/login", {"user": "ana",
                                      "password": "correct horse"})
        status, body = self.post("/api/auth/change-password",
                                 {"old_password": "nope",
                                  "new_password": "y" * 8})
        self.assertEqual((status, body["error"]),
                         (403, "current password incorrect"))
        status, body = self.post("/api/auth/change-password",
                                 {"old_password": "correct horse",
                                  "new_password": "short"})
        self.assertEqual(status, 400)
        status, body = self.post("/api/auth/change-password",
                                 {"old_password": "correct horse",
                                  "new_password": "battery staple"})
        self.assertEqual(body, {"ok": True, "user": "ana"})
        # The session stays valid; the new password works.
        self.assertEqual(self.get("/api/cousins")[0], 200)
        users = auth.Users(self.root / "config" / "console-users.json")
        self.assertTrue(users.verify("ana", "battery staple"))

    def test_static_files_need_no_session(self):
        self.serve()
        static = self.root / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html>")
        self.server.static_dir = static
        self.assertEqual(self.get("/", raw=True)[0], 200)


class TestAdduserCli(ConsoleCase):
    def test_adduser_prompts_and_writes_the_file(self):
        from cousin_lib.console.app import console_main
        with mock.patch("getpass.getpass",
                        side_effect=["pw pw pw pw", "pw pw pw pw"]):
            rc = console_main(["--root", str(self.root), "adduser", "ana"])
        self.assertEqual(rc, 0)
        users = auth.Users(self.root / "config" / "console-users.json")
        self.assertTrue(users.verify("ana", "pw pw pw pw"))

    def test_adduser_from_inside_a_checkout_needs_no_root(self):
        import contextlib
        import os
        from cousin_lib.console.app import console_main
        (self.root / "templates").mkdir(exist_ok=True)
        (self.root / "templates" / "cousin-CLAUDE.template.md").write_text(
            "x")
        (self.root / "config").mkdir(exist_ok=True)
        with mock.patch.dict(os.environ):
            os.environ.pop("FRAMEWORK_ROOT", None)
            with contextlib.chdir(self.root), \
                    mock.patch("getpass.getpass",
                               side_effect=["pw pw pw pw", "pw pw pw pw"]):
                rc = console_main(["adduser", "ana"])
        self.assertEqual(rc, 0)
        users = auth.Users(self.root / "config" / "console-users.json")
        self.assertTrue(users.verify("ana", "pw pw pw pw"))

    def test_adduser_refuses_a_mismatch_or_a_short_password(self):
        from cousin_lib.console.app import console_main
        with mock.patch("getpass.getpass", side_effect=["aaaaaaaa", "b"]):
            self.assertEqual(
                console_main(["--root", str(self.root), "adduser", "ana"]),
                2)
        with mock.patch("getpass.getpass", side_effect=["short", "short"]):
            self.assertEqual(
                console_main(["--root", str(self.root), "adduser", "ana"]),
                2)
        self.assertFalse((self.root / "config"
                          / "console-users.json").exists())


if __name__ == "__main__":
    unittest.main()
