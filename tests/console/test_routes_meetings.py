"""Meeting routes: the console opens, posts, skips and closes; the
speaker of a user action is the signed-in user."""
import unittest
from unittest import mock

from cousin_lib import meetings
from cousin_lib.console import auth
from tests.console._harness import ConsoleCase


class MeetingRoutesCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.delivered = []
        for patch in (
                mock.patch.object(meetings, "default_is_alive",
                                  lambda slug: slug != "stopped"),
                mock.patch.object(meetings, "default_deliver",
                                  lambda slug, text: self.delivered.append(
                                      (slug, text)) or True)):
            patch.start()
            self.addCleanup(patch.stop)
        for slug in ("wren", "toki", "stopped"):
            self.cousin(slug)


class TestMeetingRoutes(MeetingRoutesCase):
    def test_open_post_show_skip_close(self):
        self.serve()
        status, body = self.post("/api/meetings", {
            "topic": "names", "participants": ["wren", "toki"]})
        self.assertEqual(status, 200, body)
        mid = body["meeting"]["id"]
        status, body = self.post("/api/meetings/%d/post" % mid,
                                 {"text": "ideas?"})
        self.assertEqual(body["meeting"]["turn_slug"], "wren")
        self.assertEqual(self.delivered[-1][0], "wren")
        status, body = self.get("/api/meetings/%d" % mid)
        self.assertEqual([e["speaker"] for e in body["meeting"]["transcript"]],
                         ["system", "user"])
        status, body = self.post("/api/meetings/%d/skip" % mid)
        self.assertEqual(body["meeting"]["turn_slug"], "toki")
        status, body = self.post("/api/meetings/%d/close" % mid)
        self.assertEqual(body["meeting"]["state"], "closed")
        status, body = self.get("/api/meetings")
        self.assertEqual([m["id"] for m in body["meetings"]], [mid])
        self.assertEqual(self.delete("/api/meetings/%d" % mid),
                         (200, {"ok": True, "deleted": mid}))
        self.assertEqual(self.get("/api/meetings/%d" % mid)[0], 404)

    def test_refusals_are_400_and_unknown_is_404(self):
        self.serve()
        status, body = self.post("/api/meetings", {
            "topic": "x", "participants": ["wren", "stopped"]})
        self.assertEqual((status, body["error"]),
                         (400, "not running: stopped (start them first)"))
        self.assertEqual(self.post("/api/meetings", {
            "topic": "x", "participants": "wren"})[0], 400)
        self.assertEqual(self.get("/api/meetings/99")[0], 404)
        self.assertEqual(self.post("/api/meetings/99/post",
                                   {"text": "x"})[0], 404)


class TestSpeakerIsTheSignedInUser(MeetingRoutesCase):
    def test_user_entries_carry_the_login_name(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.serve()
        self.assertEqual(self.get("/api/meetings")[0], 401)
        self.post("/api/auth/login", {"user": "ana",
                                      "password": "correct horse"})
        mid = self.post("/api/meetings", {
            "topic": "names", "participants": ["wren"]})[1]["meeting"]["id"]
        self.post("/api/meetings/%d/post" % mid, {"text": "hi"})
        transcript = self.get("/api/meetings/%d" % mid)[1]["meeting"][
            "transcript"]
        self.assertEqual(transcript[-1]["speaker"], "ana")
        self.assertIn("ana: hi", self.delivered[-1][1])


if __name__ == "__main__":
    unittest.main()
